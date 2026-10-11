"""Webhook ingress: the reflex arc's entry point (spec §4.1.5, §5.6).

    neatlogs ──HTTPS POST──▶ /hook/<256-bit secret>
       1. verify   secret path (constant time) + X-Neatlogs-Signature + optional source allowlist
       2. dedupe   event id seen in the last 24 h → 200, dropped
       3. persist  the raw body under evidence/ingress/<day>/, its SHA-256 recorded
       4. enqueue  {event_id, type, action, detection_id, hit_keys, trace_ids, received_at}
       5. answer   200 (all local work; nothing waits on the control plane)

Verified delivery format (spike A6): ``X-Neatlogs-Signature: sha256=<hex HMAC-SHA256(secret,
raw body)>``, ``X-Neatlogs-Event`` and ``X-Neatlogs-Delivery-Id`` headers; the body's ``id`` is
the event id and ``data.detection`` carries the detection id and span id. The signed material has
no timestamp, so replay protection is the event-id dedupe. neatlogs did not retry a delivery
that got HTTP 500, and replaying deliveries needs an OAuth user token, so a lost delivery is
recovered by the poller, which claims hits in the same table: a hit is processed once whichever
path sees it first, and a replayed delivery is suppressed by its event id and its hit keys.

The ``available`` switch answers 503 while off; the Watchdog's chaos API drives it for the L2
outage drill (§6.6).
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from kintsugi.senses.store import HitEvent, SenseStore, hit_key

MAX_BODY_BYTES = 1 << 20
SIGNATURE_HEADER = "x-neatlogs-signature"

# §4.1.5: what the Sentinel does with each alert event.
SENTINEL_ACTIONS: Mapping[str, str] = {
    "alert.per_event_match": "open_or_attach",
    "alert.threshold_breach": "open_with_severity",
    "alert.metric_threshold_breach": "open_with_severity",
    "alert.anomaly_detected": "open_anomaly_notify_governor",
    "alert.compound_breach": "open_compound_cluster",
    "alert.sla_breach_warning": "raise_host_priority",
    "alert.ingestion_silent": "availability_or_watchdog",
}
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def sign(secret: str, body: bytes) -> str:
    """The signature neatlogs sends for ``body`` (spike A6)."""
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def action_for(event_type: str, payload: Mapping[str, Any] | None = None) -> str:
    action = SENTINEL_ACTIONS.get(event_type, "record_only")
    if action == "availability_or_watchdog":
        data = (payload or {}).get("data") or {}
        trace = data.get("trace") if isinstance(data, Mapping) else None
        workflow = str((trace or {}).get("workflowName") or data.get("workflowName") or "")
        return "wake_watchdog" if workflow.startswith("kintsugi:") else "open_availability"
    return action


@dataclass(frozen=True)
class ParsedEvent:
    event_id: str
    type: str
    delivery_id: str | None
    detection_id: str | None
    hits: tuple[tuple[str, str], ...]  # (trace_id, span_id)
    trace_ids: tuple[str, ...]
    severity: str | None


def parse_event(payload: Mapping[str, Any]) -> ParsedEvent:
    """The fields Omega needs from an alert delivery; tolerant of missing optional parts."""
    event_id = payload.get("id")
    event_type = payload.get("type")
    if not isinstance(event_id, str) or not isinstance(event_type, str):
        raise ValueError("an event needs string id and type")
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
    assert isinstance(data, Mapping)
    detection = data.get("detection") if isinstance(data.get("detection"), Mapping) else {}
    assert isinstance(detection, Mapping)
    detection_id = detection.get("id") if isinstance(detection.get("id"), str) else None
    trace_id = data.get("traceId") or data.get("primaryTraceId")
    span_id = detection.get("spanId")
    hits: tuple[tuple[str, str], ...] = ()
    if detection_id and isinstance(trace_id, str) and isinstance(span_id, str):
        hits = ((trace_id, span_id),)
    samples = data.get("sampleTraceIds") if isinstance(data.get("sampleTraceIds"), list) else []
    assert isinstance(samples, list)
    traces = tuple(dict.fromkeys([t for t in [trace_id, *samples] if isinstance(t, str)]))
    delivery = payload.get("deliveryId")
    severity = data.get("severity")
    return ParsedEvent(event_id, event_type, delivery if isinstance(delivery, str) else None,
                       detection_id, hits, traces,
                       severity if isinstance(severity, str) else None)  # fmt: skip


@dataclass
class IngressConfig:
    path_secret: str
    signing_secret: str | None
    evidence_dir: Path
    bindings: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    allow_sources: Sequence[str] = ()  # CIDRs; empty allows any source
    source_header: str | None = None  # e.g. cf-connecting-ip behind a tunnel
    max_body_bytes: int = MAX_BODY_BYTES
    admin_token: str | None = None  # the Watchdog's chaos API toggles availability with it

    def __post_init__(self) -> None:
        if len(self.path_secret) < 43:  # 256 bits as base64url
            raise ValueError("the path secret must carry at least 256 bits (43 base64url chars)")
        if self.admin_token is not None and len(self.admin_token) < 43:
            raise ValueError("the admin token must carry at least 256 bits")


@dataclass
class IngressState:
    available: bool = True
    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0
    hits_claimed: int = 0
    hits_suppressed: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def count(self, name: str, n: int = 1) -> None:
        with self.lock:
            setattr(self, name, getattr(self, name) + n)


def _source_allowed(config: IngressConfig, request: Request) -> bool:
    if not config.allow_sources:
        return True
    raw = request.headers.get(config.source_header) if config.source_header else None
    raw = raw or (request.client.host if request.client else "")
    try:
        address = ipaddress.ip_address(raw.split(",")[0].strip())
    except ValueError:
        return False
    return any(address in ipaddress.ip_network(cidr, strict=False) for cidr in config.allow_sources)


def persist(evidence_dir: Path, event_id: str, body: bytes, received_at: float) -> tuple[str, Path]:
    digest = hashlib.sha256(body).hexdigest()
    day = datetime.fromtimestamp(received_at, UTC).strftime("%Y-%m-%d")
    path = evidence_dir / "ingress" / day / f"{_SAFE.sub('_', event_id)}-{digest[:12]}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(body)
    return digest, path


def create_app(
    config: IngressConfig,
    store: SenseStore,
    *,
    state: IngressState | None = None,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    state = state or IngressState()
    app = FastAPI(title="kintsugi-ingress", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ingress = state

    def reject(status: int, reason: str) -> JSONResponse:
        state.count("rejected")
        return JSONResponse({"status": "rejected", "reason": reason}, status_code=status)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "available": state.available, "accepted": state.accepted,
                "duplicates": state.duplicates, "queue": store.pending()}  # fmt: skip

    @app.post("/admin/availability")
    async def availability(request: Request) -> JSONResponse:
        """The Watchdog's chaos API: switch the ingress off (503) and on again (§5.5, §6.6)."""
        expected = f"Bearer {config.admin_token}" if config.admin_token else None
        given = request.headers.get("authorization", "")
        if expected is None or not hmac.compare_digest(given.encode(), expected.encode()):
            return reject(401, "unauthorized")
        body = await request.json()
        state.available = bool(body.get("available"))
        return JSONResponse({"available": state.available})

    @app.post("/hook/{secret}")
    async def hook(secret: str, request: Request) -> JSONResponse:
        if not state.available:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        if not hmac.compare_digest(secret.encode(), config.path_secret.encode()):
            return reject(404, "not found")
        if not _source_allowed(config, request):
            return reject(403, "source not allowed")
        body = await request.body()
        if len(body) > config.max_body_bytes:
            return reject(413, "payload too large")
        if config.signing_secret is not None:
            given = request.headers.get(SIGNATURE_HEADER, "")
            if not hmac.compare_digest(given.encode(), sign(config.signing_secret, body).encode()):
                return reject(401, "bad signature")
        try:
            payload = json.loads(body)
            event = parse_event(payload)
        except (ValueError, TypeError):
            return reject(400, "malformed event")
        return accept(event, payload, body, request.headers.get("x-neatlogs-delivery-id"))

    def accept(
        event: ParsedEvent, payload: Mapping[str, Any], body: bytes, delivery: str | None
    ) -> JSONResponse:
        received = clock()
        digest = hashlib.sha256(body).hexdigest()
        if not store.claim_event(event.event_id, event.type, digest,
                                 delivery_id=delivery or event.delivery_id):  # fmt: skip
            state.count("duplicates")
            return JSONResponse({"status": "duplicate", "event_id": event.event_id})
        _, path = persist(config.evidence_dir, event.event_id, body, received)
        binding = config.bindings.get(event.detection_id or "", {})
        claimed: list[str] = []
        for trace_id, span_id in event.hits:
            assert event.detection_id is not None
            hit = HitEvent(
                hit_key=hit_key(event.detection_id, trace_id, span_id),
                detection_id=event.detection_id,
                trace_id=trace_id,
                span_id=span_id,
                source="webhook",
                receptor=binding.get("receptor"),
                host=binding.get("host"),
                event_id=event.event_id,
            )
            if store.claim_hit(hit):
                claimed.append(hit.hit_key)
            else:
                state.count("hits_suppressed")
        state.count("hits_claimed", len(claimed))
        store.enqueue_alert({
            "event_id": event.event_id,
            "type": event.type,
            "action": action_for(event.type, payload),
            "detection_id": event.detection_id,
            "receptor": binding.get("receptor"),
            "host": binding.get("host"),
            "hit_keys": claimed,
            "trace_ids": list(event.trace_ids),
            "severity": event.severity,
            "received_at": received,
            "payload_sha256": digest,
            "evidence": path.as_posix(),
        })  # fmt: skip
        state.count("accepted")
        return JSONResponse({"status": "accepted", "event_id": event.event_id,
                             "new_hits": len(claimed)})  # fmt: skip

    return app
