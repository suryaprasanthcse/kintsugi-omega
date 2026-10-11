"""Invariant I7 (completeness) and the primitive of audit check C9 (spec §4.1.7, §7.5).

Every detection hit inside the window W must map to the ledger: either it **opened** an incident
or it was **attached** to one (a dedupe), or it was **held** below its receptor's paging
threshold against a watch record (a dedupe too; amendment A-47), as recorded in ``seen_hits``.
A hit that neatlogs
reports but Omega never claimed is *missing*; a claimed hit with no decision is *pending*. Either
fails I7. The check is a pure function over the hits neatlogs reports and the store's rows, so
the audit can run it online (hits from the API) or offline (hits from a published bundle).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from kintsugi.senses.neatlogs_api import Hit
from kintsugi.senses.store import SeenHit, hit_key


@dataclass(frozen=True)
class ReportedHit:
    detection_id: str
    trace_id: str
    span_id: str
    detected_at: datetime

    @property
    def key(self) -> str:
        return hit_key(self.detection_id, self.trace_id, self.span_id)


@dataclass
class CompletenessReport:
    start: datetime
    end: datetime
    total: int = 0
    incidents: set[str] = field(default_factory=set)
    opened: int = 0
    attached: int = 0
    held: int = 0  # below the paging threshold, recorded against a watch record (A-47)
    pending: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.pending and not self.missing

    def summary(self) -> str:
        verdict = "PASS" if self.ok else "FAIL"
        mapped = f"{self.total} hits -> {len(self.incidents)} incidents + {self.attached} dedupes"
        if self.held:
            mapped += f" + {self.held} held below threshold"
        return f"{mapped} ({len(self.pending)} pending, {len(self.missing)} missing) {verdict}"


def completeness(
    reported: Iterable[ReportedHit],
    seen: Mapping[str, SeenHit],
    start: datetime,
    end: datetime,
) -> CompletenessReport:
    report = CompletenessReport(start, end)
    keys: set[str] = set()
    for hit in reported:
        if not start <= hit.detected_at < end or hit.key in keys:
            continue
        keys.add(hit.key)
        report.total += 1
        row = seen.get(hit.key)
        if row is None:
            report.missing.append(hit.key)
        elif row.incident is None or row.disposition is None:
            report.pending.append(hit.key)
        elif row.disposition == "held":  # below the paging threshold: a dedupe record (A-47)
            report.held += 1
        else:
            report.incidents.add(row.incident)
            if row.disposition == "opened":
                report.opened += 1
            else:
                report.attached += 1
    return report


class _HitPages(Protocol):
    def hit_pages(
        self, detection_id: str, *, limit: int = 100
    ) -> Iterator[tuple[list[Hit], Any]]: ...


def reported_hits(
    api: _HitPages, detection_ids: Iterable[str], start: datetime
) -> list[ReportedHit]:
    """Every hit neatlogs reports for ``detection_ids`` detected at or after ``start``."""
    out: list[ReportedHit] = []
    for detection_id in detection_ids:
        for hits, _page in api.hit_pages(detection_id):
            out.extend(ReportedHit(detection_id, h.trace_id, h.span_id, h.detected_at)
                       for h in hits)  # fmt: skip
            if hits and min(h.detected_at for h in hits) < start:
                break
    return out
