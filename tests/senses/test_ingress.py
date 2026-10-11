"""The webhook ingress (spec §5.6): verify, dedupe with 200, persist, enqueue, answer fast.

The delivery body is the real ``alert.per_event_match`` neatlogs sent in spike A6 (byte for
byte); tests sign it with their own secret, exactly as neatlogs signs with the webhook's.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from kintsugi.senses.ingress import (
    SENTINEL_ACTIONS,
    IngressConfig,
    IngressState,
    action_for,
    create_app,
    parse_event,
    sign,
)
from kintsugi.senses.store import SenseStore, hit_key

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "neatlogs"
PATH_SECRET = "p" * 43
SIGNING = "s" * 43
DETECTION = "7c58bc63-7f3b-4eac-a3a2-0293657f0937"
TRACE = "8e7e60e9b5bd6e0107af2ec0f8ced89f"
SPAN = "80b8bf2ed942c6a3"


def recorded() -> tuple[dict[str, str], bytes]:
    data = json.loads((FIXTURES / "webhook_delivery_per_event.json").read_text(encoding="utf-8"))
    return data["headers"], data["body"].encode("utf-8")


def variant(body: bytes, **changes: Any) -> bytes:
    payload = json.loads(body)
    payload.update(changes)
    return json.dumps(payload).encode("utf-8")


@pytest.fixture
def ingress(tmp_path: Path):
    store = SenseStore(tmp_path / "senses.sqlite")
    state = IngressState()
    config = IngressConfig(
        path_secret=PATH_SECRET,
        signing_secret=SIGNING,
        evidence_dir=tmp_path / "evidence",
        bindings={DETECTION: {"receptor": "R-007", "host": "ledger"}},
    )
    client = TestClient(create_app(config, store, state=state))
    return client, store, state, tmp_path


def post(client: TestClient, body: bytes, *, secret: str = SIGNING, path: str = PATH_SECRET):
    headers, _ = recorded()
    headers = {**headers, "x-neatlogs-signature": sign(secret, body)}
    return client.post(f"/hook/{path}", content=body, headers=headers)


def test_a_signed_delivery_is_accepted_persisted_and_enqueued(ingress) -> None:
    client, store, state, tmp = ingress
    _, body = recorded()
    response = post(client, body)
    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "event_id": json.loads(body)["id"],
                               "new_hits": 1}  # fmt: skip
    key = hit_key(DETECTION, TRACE, SPAN)
    seen = store.seen(key)
    assert seen is not None
    assert (seen.source, seen.receptor, seen.host) == ("webhook", "R-007", "ledger")
    items = store.take()
    alert = next(i for i in items if i.kind == "alert").payload
    assert alert["action"] == "open_or_attach"
    assert alert["hit_keys"] == [key]
    assert alert["trace_ids"] == [TRACE]
    evidence = Path(alert["evidence"])
    assert evidence.read_bytes() == body
    assert alert["payload_sha256"] == hashlib.sha256(body).hexdigest()
    assert evidence.is_relative_to(tmp / "evidence" / "ingress")
    assert [i.kind for i in items] == ["hit", "alert"]
    assert state.accepted == 1


def test_a_duplicate_event_answers_200_and_is_dropped(ingress) -> None:
    client, store, state, _ = ingress
    _, body = recorded()
    assert post(client, body).json()["status"] == "accepted"
    store.take()
    again = post(client, body)
    assert again.status_code == 200
    assert again.json()["status"] == "duplicate"
    assert store.take() == []  # nothing new for the control plane
    assert state.duplicates == 1
    assert store.event_duplicates() == 1


def test_a_replayed_delivery_is_suppressed_by_event_id_and_by_hit_key(ingress) -> None:
    client, store, state, _ = ingress
    _, body = recorded()
    post(client, body)
    store.take()
    replay = variant(body, deliveryId="11111111-2222-4333-8444-555555555555")
    assert post(client, replay).json()["status"] == "duplicate"
    # Even a new event id for the same hit (a second alert on one span) claims nothing twice.
    other = variant(body, id="alert:00000000-0000-4000-8000-000000000001")
    response = post(client, other)
    assert response.json() == {"status": "accepted", "event_id": json.loads(other)["id"],
                               "new_hits": 0}  # fmt: skip
    assert [i.kind for i in store.take()] == ["alert"]
    assert state.hits_suppressed == 1


def test_signature_secret_path_source_and_shape_are_enforced(ingress, tmp_path: Path) -> None:
    client, store, state, _ = ingress
    _, body = recorded()
    assert post(client, body, secret="wrong" * 9).status_code == 401
    assert post(client, body, path="q" * 43).status_code == 404
    unsigned = client.post(f"/hook/{PATH_SECRET}", content=body)
    assert unsigned.status_code == 401
    assert post(client, b"not json").status_code == 400
    assert post(client, json.dumps({"type": "alert.per_event_match"}).encode()).status_code == 400
    assert store.take() == []
    assert state.rejected == 5

    allow = IngressConfig(path_secret=PATH_SECRET, signing_secret=SIGNING,
                          evidence_dir=tmp_path / "ev2", allow_sources=["203.0.113.0/24"],
                          source_header="cf-connecting-ip")  # fmt: skip
    gated = TestClient(create_app(allow, SenseStore(tmp_path / "other.sqlite")))
    headers = {"x-neatlogs-signature": sign(SIGNING, body)}
    ok = gated.post(f"/hook/{PATH_SECRET}", content=body,
                    headers={**headers, "cf-connecting-ip": "203.0.113.9"})  # fmt: skip
    assert ok.status_code == 200
    denied = gated.post(f"/hook/{PATH_SECRET}", content=body,
                        headers={**headers, "cf-connecting-ip": "198.51.100.7"})  # fmt: skip
    assert denied.status_code == 403


def test_a_short_path_secret_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="256 bits"):
        IngressConfig(path_secret="short", signing_secret=None, evidence_dir=tmp_path)


def test_the_outage_switch_answers_503_and_records_nothing(ingress) -> None:
    client, store, state, _ = ingress
    _, body = recorded()
    state.available = False
    assert post(client, body).status_code == 503
    assert store.take() == []
    state.available = True
    assert post(client, body).json()["status"] == "accepted"


def test_ingress_answers_within_two_seconds(ingress) -> None:
    client, _, _, _ = ingress
    _, body = recorded()
    worst = 0.0
    for n in range(40):
        started = time.perf_counter()
        response = post(client, variant(body, id=f"alert:00000000-0000-4000-8000-{n:012d}"))
        worst = max(worst, time.perf_counter() - started)
        assert response.status_code == 200
    assert worst < 2.0


@pytest.mark.parametrize("event_type", sorted(SENTINEL_ACTIONS))
def test_every_alert_event_maps_to_a_sentinel_action(ingress, event_type: str) -> None:
    client, store, _, _ = ingress
    _, body = recorded()
    payload = json.loads(body)
    payload["type"] = event_type
    payload["data"]["type"] = event_type
    response = post(client, json.dumps(payload).encode())
    assert response.status_code == 200
    alert = next(i for i in store.take() if i.kind == "alert").payload
    assert alert["type"] == event_type
    assert alert["action"] == action_for(event_type, payload)
    assert alert["action"] != "record_only"


def test_ingestion_silence_on_omega_wakes_the_watchdog() -> None:
    crew = {"data": {"trace": {"workflowName": "kintsugi:surgeon"}}}
    host = {"data": {"trace": {"workflowName": "host:ledger"}}}
    assert action_for("alert.ingestion_silent", crew) == "wake_watchdog"
    assert action_for("alert.ingestion_silent", host) == "open_availability"
    assert action_for("notification.daily_digest") == "record_only"


def test_the_recorded_delivery_parses_to_one_hit() -> None:
    _, body = recorded()
    event = parse_event(json.loads(body))
    assert event.type == "alert.per_event_match"
    assert event.detection_id == DETECTION
    assert event.hits == ((TRACE, SPAN),)
    assert event.event_id.startswith("alert:")
