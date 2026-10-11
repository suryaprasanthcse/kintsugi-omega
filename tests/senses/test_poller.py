"""The reconciliation poller and the shared store: exactly-once hits across both paths (§4.1.7).

The L2 drill in miniature (§6.6): hits arrive while the ingress answers 503; the poller recovers
them by sweeping; the deliveries replayed afterwards are suppressed; no hit is processed twice.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from kintsugi.senses.detections import Binding, MetricReceptor
from kintsugi.senses.ingress import IngressConfig, IngressState, create_app, sign
from kintsugi.senses.neatlogs_api import CursorExpired, Hit, Page, TraceSummary
from kintsugi.senses.poller import TRACE_SPAN, Poller
from kintsugi.senses.store import EVENT_WINDOW_S, HitEvent, QueueSink, SenseStore, hit_key

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "neatlogs"
DETECTION = "7c58bc63-7f3b-4eac-a3a2-0293657f0937"
NOW = datetime(2026, 10, 11, 15, 0, tzinfo=UTC)
PATH_SECRET, SIGNING = "p" * 43, "s" * 43


def make_hit(n: int, minutes_ago: float) -> Hit:
    return Hit(
        id=f"00000000-0000-4000-8000-{n:012d}",
        detected_at=NOW - timedelta(minutes=minutes_ago),
        trace_id=f"{n:032x}",
        span_id=f"{n:016x}",
        score=1.0,
    )


class HitsApi:
    """Serves hits newest first in pages, like ``GET /detections/{id}/hits``."""

    def __init__(self, hits: list[Hit], page: int = 3, expire_after: int | None = None) -> None:
        self.hits = sorted(hits, key=lambda h: h.detected_at, reverse=True)
        self.page = page
        self.expire_after = expire_after
        self.pages_served = 0

    def hit_pages(self, detection_id: str, *, limit: int = 100):
        for start in range(0, max(len(self.hits), 1), self.page):
            self.pages_served += 1
            if self.expire_after is not None and self.pages_served > self.expire_after:
                self.expire_after = None
                raise CursorExpired("detections.hits", 400, "PUBLIC_API_INVALID_CURSOR", "x")
            chunk = self.hits[start : start + self.page]
            more = start + self.page < len(self.hits)
            yield chunk, Page(has_more=more, limit=self.page, next_cursor="c" if more else None)
            if not more:
                return


def make_poller(api: Any, store: SenseStore, **kw: Any) -> tuple[Poller, QueueSink]:
    sink = QueueSink()
    bindings = {DETECTION: Binding(DETECTION, "R-007", "ledger", 1, "rule-1")}
    return Poller(api, store, sink, bindings, now=lambda: NOW, **kw), sink


def test_each_hit_is_claimed_once_whichever_path_sees_it_first(tmp_path: Path) -> None:
    store = SenseStore(tmp_path / "s.sqlite")
    hits = [make_hit(n, n) for n in range(1, 8)]
    # The webhook saw two of them first.
    for h in hits[:2]:
        key = hit_key(DETECTION, h.trace_id, h.span_id)
        ev = HitEvent(key, DETECTION, h.trace_id, h.span_id, "webhook")
        assert store.claim_hit(ev)
    poller, sink = make_poller(HitsApi(hits), store)
    report = poller.sweep(DETECTION)
    assert (report.seen, report.new, report.duplicates) == (7, 5, 2)
    assert len(sink.emitted) == 5
    assert poller.sweep(DETECTION).new == 0  # a second sweep claims nothing
    first = store.seen(hit_key(DETECTION, hits[0].trace_id, hits[0].span_id))
    assert first is not None
    assert first.source == "webhook"
    assert first.hit_id == hits[0].id  # the poller filled in the id the webhook lacked
    queued = [i for i in store.take() if i.kind == "hit"]
    assert len(queued) == 7  # one queue item per hit, never two


def test_concurrent_claims_never_double_process(tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite"
    SenseStore(path)
    wins: list[int] = []

    def claimer(source: str) -> None:
        store = SenseStore(path)
        n = 0
        for i in range(150):
            key = hit_key(DETECTION, f"t{i}", "s")
            n += store.claim_hit(HitEvent(key, DETECTION, f"t{i}", "s", source))  # type: ignore[arg-type]
        wins.append(n)

    threads = [threading.Thread(target=claimer, args=(s,)) for s in ("webhook", "poller") * 2]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(wins) == 150
    assert len(SenseStore(path).seen_hits()) == 150


def test_the_l2_drill_poller_recovers_the_outage_and_replays_are_suppressed(tmp_path: Path) -> None:
    store = SenseStore(tmp_path / "s.sqlite")
    state = IngressState()
    config = IngressConfig(path_secret=PATH_SECRET, signing_secret=SIGNING,
                           evidence_dir=tmp_path / "ev")  # fmt: skip
    client = TestClient(create_app(config, store, state=state))
    recorded = json.loads((FIXTURES / "webhook_delivery_per_event.json").read_text("utf-8"))
    template = json.loads(recorded["body"])
    hits = [make_hit(n, 10 - n) for n in range(1, 4)]

    def delivery(h: Hit, n: int) -> bytes:
        body = json.loads(json.dumps(template))
        body["id"] = f"alert:00000000-0000-4000-8000-{n:012d}"
        body["data"]["traceId"] = h.trace_id
        body["data"]["detection"]["id"] = DETECTION
        body["data"]["detection"]["spanId"] = h.span_id
        return json.dumps(body).encode()

    def post(body: bytes) -> int:
        headers = {"x-neatlogs-signature": sign(SIGNING, body)}
        return client.post(f"/hook/{PATH_SECRET}", content=body, headers=headers).status_code

    state.available = False  # S-18: the webhook outage
    assert [post(delivery(h, n)) for n, h in enumerate(hits)] == [503, 503, 503]
    assert store.seen_hits() == {}

    poller, sink = make_poller(HitsApi(hits), store)
    assert poller.sweep(DETECTION).new == 3  # the poller is the source of truth
    assert {e.trace_id for e in sink.emitted} == {h.trace_id for h in hits}

    state.available = True  # ingress recovers; neatlogs deliveries are replayed
    assert [post(delivery(h, n)) for n, h in enumerate(hits)] == [200, 200, 200]
    assert state.hits_suppressed == 3
    assert state.hits_claimed == 0
    assert len([i for i in store.take() if i.kind == "hit"]) == 3  # still exactly three


def test_a_restart_resumes_from_the_watermark_and_stops_early(tmp_path: Path) -> None:
    store = SenseStore(tmp_path / "s.sqlite")
    old = [make_hit(n, 300 + n) for n in range(1, 31)]  # five hours ago
    api = HitsApi(old)
    poller, _ = make_poller(api, store)
    assert poller.sweep(DETECTION).new == 30
    # The process is down; four new hits arrive; it restarts with only its watermark.
    fresh = [make_hit(100 + n, n) for n in range(1, 5)]
    api2 = HitsApi(fresh + old)
    poller2, sink2 = make_poller(api2, SenseStore(tmp_path / "s.sqlite"))
    report = poller2.sweep(DETECTION)
    assert report.new == 4
    assert {e.hit_id for e in sink2.emitted} == {h.id for h in fresh}
    assert report.pages < 34 / 3  # stopped once a page was older than watermark - overlap


def test_an_expired_cursor_restarts_the_sweep_without_double_claims(tmp_path: Path) -> None:
    store = SenseStore(tmp_path / "s.sqlite")
    hits = [make_hit(n, n) for n in range(1, 10)]
    poller, sink = make_poller(HitsApi(hits, expire_after=2), store)
    report = poller.sweep(DETECTION)
    assert report.restarts == 1
    assert report.new == 9
    assert len(sink.emitted) == 9


class TraceApi:
    def __init__(self, summaries: list[TraceSummary]) -> None:
        self.summaries = summaries

    def traces(self, **params: Any):
        assert params["workflow"] == "host:ledger"
        yield from self.summaries


def summary(n: int, tools: int, *, finalized: bool = True) -> TraceSummary:
    base = json.loads((FIXTURES / "trace_get.json").read_text("utf-8"))["body"]["data"]
    return TraceSummary.model_validate({
        **base, "traceId": f"{n:032x}", "toolCallsCount": tools,
        "startedAt": (NOW - timedelta(minutes=n)).isoformat(),
        "finalizationStatus": "finalized" if finalized else "pending",
    })  # fmt: skip


def test_metric_receptors_fire_on_finalized_traces_once(tmp_path: Path) -> None:
    store = SenseStore(tmp_path / "s.sqlite")
    receptor = MetricReceptor("R-002", "ledger", 1, "tool_calls", ">", 54)
    traces = [summary(1, 80), summary(2, 27), summary(3, 90, finalized=False)]
    poller, sink = make_poller(TraceApi(traces), store)
    poller.metrics = [receptor]
    first = poller.evaluate_metric(receptor)
    assert first.new == 1
    hit = sink.emitted[0]
    assert (hit.receptor, hit.host, hit.span_id, hit.source) == ("R-002", "ledger", TRACE_SPAN,
                                                                 "metric")  # fmt: skip
    assert hit.score == 80
    assert poller.evaluate_metric(receptor).new == 0
    assert store.watermark(receptor.detection_id) is not None


def test_event_dedupe_holds_for_24_hours_then_expires(tmp_path: Path) -> None:
    clock = [1_000_000.0]
    store = SenseStore(tmp_path / "s.sqlite", clock=lambda: clock[0])
    assert store.claim_event("alert:1", "alert.per_event_match", "h")
    clock[0] += EVENT_WINDOW_S - 1
    assert not store.claim_event("alert:1", "alert.per_event_match", "h")
    clock[0] += 2
    assert store.claim_event("alert:1", "alert.per_event_match", "h")
    assert store.prune_events() == 0


def test_watermarks_only_move_forward(tmp_path: Path) -> None:
    store = SenseStore(tmp_path / "s.sqlite")
    store.set_watermark("d", "2026-10-11T10:00:00.000Z")
    store.set_watermark("d", "2026-10-11T09:00:00.000Z")
    store.set_watermark("d", None)
    assert store.watermark("d") == "2026-10-11T10:00:00.000Z"


def test_assign_records_the_sentinel_decision(tmp_path: Path) -> None:
    store = SenseStore(tmp_path / "s.sqlite")
    store.claim_hit(HitEvent("k", DETECTION, "t", "s", "poller"))
    store.assign("k", "INC-0042", "opened")
    seen = store.seen("k")
    assert seen is not None
    assert (seen.incident, seen.disposition) == ("INC-0042", "opened")
    with pytest.raises(KeyError):
        store.assign("missing", "INC-0042", "attached")
