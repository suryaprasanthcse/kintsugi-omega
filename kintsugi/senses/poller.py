"""The reconciliation poller: the source of truth for hits (spec §4.1.7).

Every 60 s, for each active detection, the poller sweeps the detection's hits and claims each in
the shared ``seen_hits`` table; only hits no path has claimed reach the control plane. Hits are
listed newest first and cursors expire 15 minutes after a chain starts (spike A11), so the spec's
persistent per-detection cursor becomes a persisted **watermark**: each sweep starts a fresh
chain at the newest hit and pages back until a whole page is older than the watermark minus an
overlap. A process down for hours resumes from its watermark and recovers every hit in between;
a cursor that expires mid-sweep restarts the sweep, and the claims make the restart harmless.

Omega's own metric receptors (R-002, R-003, R-004) are evaluated in the same loop over trace
summaries (``GET /traces?workflowName=host:<id>&from=…``), keyed per trace under the receptor's
pseudo detection id. Only finalized traces are judged, so a trace's counts are final.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from kintsugi.senses.detections import Binding, MetricReceptor
from kintsugi.senses.neatlogs_api import CursorExpired, Hit, Page, TraceSummary
from kintsugi.senses.store import HitEvent, HitSink, SenseStore, hit_key

TRACE_SPAN = "-"  # the span id of a trace-level (metric) hit


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


@dataclass
class SweepReport:
    detection_id: str
    pages: int = 0
    seen: int = 0
    new: int = 0
    duplicates: int = 0
    restarts: int = 0
    newest: str | None = None
    new_keys: list[str] = field(default_factory=list)


class Poller:
    def __init__(
        self,
        api: Any,
        store: SenseStore,
        sink: HitSink,
        bindings: Mapping[str, Binding],
        metrics: Sequence[MetricReceptor] = (),
        *,
        overlap: timedelta = timedelta(minutes=10),
        metric_overlap: timedelta = timedelta(minutes=15),
        first_sweep: timedelta = timedelta(hours=24),
        max_pages: int = 50,
        page_limit: int = 100,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.api = api
        self.store = store
        self.sink = sink
        self.bindings = dict(bindings)
        self.metrics = list(metrics)
        self.overlap = overlap
        self.metric_overlap = metric_overlap
        self.first_sweep = first_sweep
        self.max_pages = max_pages
        self.page_limit = page_limit
        self.now = now

    # ----------------------------------------------------------------------- detections

    def sweep(self, detection_id: str) -> SweepReport:
        report = SweepReport(detection_id)
        for _ in range(3):
            try:
                self._sweep(detection_id, report)
                break
            except CursorExpired:
                report.restarts += 1
        if report.newest is not None:
            self.store.set_watermark(detection_id, report.newest)
        return report

    def _sweep(self, detection_id: str, report: SweepReport) -> None:
        mark = self.store.watermark(detection_id)
        floor = (parse_iso(mark) - self.overlap) if mark else (self.now() - self.first_sweep)
        binding = self.bindings.get(detection_id)
        pages = self.api.hit_pages(detection_id, limit=self.page_limit)
        for hits, page in pages:
            assert isinstance(page, Page)
            report.pages += 1
            for hit in hits:
                assert isinstance(hit, Hit)
                report.seen += 1
                detected = iso(hit.detected_at)
                if report.newest is None or detected > report.newest:
                    report.newest = detected
                event = HitEvent(
                    hit_key=hit_key(detection_id, hit.trace_id, hit.span_id),
                    detection_id=detection_id,
                    trace_id=hit.trace_id,
                    span_id=hit.span_id,
                    source="poller",
                    hit_id=hit.id,
                    detected_at=detected,
                    receptor=binding.receptor if binding else None,
                    host=binding.host if binding else None,
                    score=hit.score,
                )
                if self.store.claim_hit(event):
                    report.new += 1
                    report.new_keys.append(event.hit_key)
                    self.sink.emit(event)
                else:
                    report.duplicates += 1
            oldest = min((h.detected_at for h in hits), default=None)
            if oldest is not None and oldest < floor:
                return
            if report.pages >= self.max_pages:
                return

    # ----------------------------------------------------------------------- metrics

    def evaluate_metric(self, receptor: MetricReceptor) -> SweepReport:
        report = SweepReport(receptor.detection_id)
        mark = self.store.watermark(receptor.detection_id)
        start = (parse_iso(mark) - self.metric_overlap) if mark else (self.now() - self.first_sweep)
        for summary in self.api.traces(workflow=f"host:{receptor.host}", start=start):
            assert isinstance(summary, TraceSummary)
            report.seen += 1
            if summary.finalization_status not in (None, "finalized"):
                continue
            started = iso(summary.started_at)
            if report.newest is None or started > report.newest:
                report.newest = started
            if not receptor.fires(_metric(summary, receptor.field)):
                continue
            event = HitEvent(
                hit_key=hit_key(receptor.detection_id, summary.trace_id, TRACE_SPAN),
                detection_id=receptor.detection_id,
                trace_id=summary.trace_id,
                span_id=TRACE_SPAN,
                source="metric",
                detected_at=started,
                receptor=receptor.receptor,
                host=receptor.host,
                score=_metric(summary, receptor.field),
            )
            if self.store.claim_hit(event):
                report.new += 1
                report.new_keys.append(event.hit_key)
                self.sink.emit(event)
            else:
                report.duplicates += 1
        if report.newest is not None:
            self.store.set_watermark(receptor.detection_id, report.newest)
        return report

    # ----------------------------------------------------------------------- loop

    def run_once(self) -> list[SweepReport]:
        reports = [self.sweep(detection_id) for detection_id in sorted(self.bindings)]
        reports += [self.evaluate_metric(m) for m in self.metrics]
        return reports

    def run(self, stop: threading.Event, *, interval_s: float = 60.0) -> None:
        while not stop.is_set():
            started = time.monotonic()
            self.run_once()
            stop.wait(max(0.0, interval_s - (time.monotonic() - started)))


def _metric(summary: TraceSummary, name: str) -> float:
    if name == "tool_calls":
        return float(summary.tool_calls_count)
    if name == "cost_usd":
        return float(summary.total_cost_usd)
    return float(summary.duration_ms)
