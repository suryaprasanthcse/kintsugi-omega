"""The neatlogs Public API client, its pinned contract and the recorded live responses.

The fixtures under ``tests/fixtures/neatlogs`` are real responses recorded during the Stage 4
spikes (train-split Ledger traces and synthetic spike traces only). Every one must validate
against the pinned contract; the client is exercised against an in-process mock transport.
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from kintsugi.senses import contract
from kintsugi.senses.neatlogs_api import (
    ApiError,
    Conflict,
    Credentials,
    CredentialUnavailable,
    CursorExpired,
    NeatlogsApi,
    RateLimited,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "neatlogs"
PROJECT = "b15df932-5b04-473d-9986-cae0053dc04e"
WEBHOOK = "6f1bec3c-1ff0-43fd-8b08-d2633ded61d4"
DELIVERY = "01803ef0-ec95-4467-8142-b5ed6822d2e4"
DETECTION = "7c58bc63-7f3b-4eac-a3a2-0293657f0937"
TOKEN = "nl" + "sa_" + "T" * 32
OAUTH = "nl" + "oa_" + "O" * 32
REQUEST_ID = "req_" + "0" * 32


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


RESPONSES = sorted(p.stem for p in FIXTURES.glob("*.json") if p.stem.count("_delivery_") == 0)


@pytest.mark.parametrize("name", RESPONSES)
def test_every_recorded_response_matches_the_pinned_contract(name: str) -> None:
    record = fixture(name)
    contract.validate_response(record["operation"], record["status"], record["body"])


def test_the_pin_records_its_source_and_covers_every_operation() -> None:
    pin = contract.pin_record()
    assert pin["source"] == contract.OPENAPI_URL
    assert pin["openapi"] == "3.1.0"
    assert pin["full_operations"] == 285
    assert pin["pinned_operations"] == len(contract.OPERATIONS)
    text = contract.SUBSET_PATH.read_bytes()
    assert hashlib.sha256(text).hexdigest() == pin["subset_sha256"]
    for name in contract.OPERATIONS:
        op = contract.operation(name)
        assert op.operation_id.startswith(op.method)


def test_the_validator_is_not_vacuous() -> None:
    body = fixture("detection_hits")["body"]
    mutations: list[Callable[[dict[str, Any]], object]] = [
        lambda b: b["data"]["hits"][0].pop("id"),
        lambda b: b["data"]["hits"][0].__setitem__("score", 2),
        lambda b: b["data"]["hits"][0].__setitem__("detectedAt", "yesterday"),
        lambda b: b["data"]["page"].__setitem__("surprise", True),
    ]
    for mutate in mutations:
        broken = copy.deepcopy(body)
        mutate(broken)
        with pytest.raises(contract.ContractError):
            contract.validate_response("detections.hits", 200, broken)


def test_operations_that_accept_only_oauth_are_known() -> None:
    oauth_only = {name for name in contract.OPERATIONS if contract.operation(name).oauth_only}
    expected = {
        "webhooks.replay",
        "webhooks.test",
        "detections.preview",
        "detections.generate",
        "detections.backfill",
    }
    service = {"detections.hits", "traces.list", "webhooks.deliveries", "alert_rules.create"}
    assert expected <= oauth_only
    assert not service & oauth_only
    assert contract.operation("traces.patch").idempotency_key
    assert not contract.operation("traces.list").idempotency_key


# --------------------------------------------------------------------------- mock transport

Handler = Callable[[httpx.Request], httpx.Response]


def ok(body: dict[str, Any], status: int = 200, headers: dict[str, str] | None = None):
    return httpx.Response(status, json=body, headers=headers)


def problem(status: int, code: str, *, retryable: bool = False, retry_after: str | None = None):
    body = {
        "type": f"urn:neatlogs:problem:{code.lower()}",
        "title": "x",
        "status": status,
        "detail": "the public api said no",
        "code": code,
        "requestId": REQUEST_ID,
        "retryable": retryable,
    }
    headers = {"content-type": "application/problem+json"}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return httpx.Response(status, json=body, headers=headers)


class Recorder:
    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)


def make_api(
    recorder: Recorder, sleeps: list[float] | None = None, *, oauth: str | None = None
) -> NeatlogsApi:
    credentials = Credentials(project_id=PROJECT, service_token=TOKEN, oauth_token=oauth)
    return NeatlogsApi(
        credentials,
        transport=httpx.MockTransport(recorder),
        validate=True,
        sleep=(sleeps if sleeps is not None else []).append,
        rng=random.Random(0),
    )


def test_requests_carry_bearer_and_project_and_parse_the_envelope() -> None:
    headers = {
        "X-NeatLogs-RateLimit-Policy": "standard-read",
        "RateLimit-Limit": "300",
        "RateLimit-Remaining": "299",
    }
    rec = Recorder(ok(fixture("traces_list")["body"], headers=headers))
    with make_api(rec) as api:
        traces = list(api.traces(workflow="host:ledger", limit=3, max_items=3))
    request = rec.requests[0]
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["x-project-id"] == PROJECT
    assert "idempotency-key" not in request.headers  # reads reject the header
    assert request.url.path == "/api/v1/public/traces"
    assert dict(request.url.params) == {"workflowName": "host:ledger", "limit": "3"}
    assert [t.trace_name for t in traces] == [f"task:ledger-train-00{i}" for i in (2, 1, 0)]
    assert all("split:train" in t.tags for t in traces)
    assert api.rate_limits["standard-read"].remaining == 299


def test_pagination_follows_the_cursor_until_has_more_is_false() -> None:
    first = fixture("detection_hits")["body"]
    page1 = copy.deepcopy(first)
    page1["data"]["hits"] = page1["data"]["hits"][:1]
    page1["data"]["page"] = {"hasMore": True, "limit": 1, "nextCursor": "nlc1.opaque"}
    page2 = copy.deepcopy(first)
    page2["data"]["hits"] = page2["data"]["hits"][1:]
    page2["data"]["page"] = {"hasMore": False, "limit": 1, "nextCursor": None}
    rec = Recorder(ok(page1), ok(page2))
    with make_api(rec) as api:
        hits = list(api.hits(DETECTION))
    assert [h.id for h in hits] == [h["id"] for h in first["data"]["hits"]]
    assert "cursor" not in rec.requests[0].url.params
    assert rec.requests[1].url.params["cursor"] == "nlc1.opaque"


def test_a_429_waits_at_least_retry_after_then_retries() -> None:
    sleeps: list[float] = []
    rec = Recorder(
        problem(429, "RATE_LIMITED", retryable=True, retry_after="6"),
        ok(fixture("trace_sharing")["body"]),
    )
    with make_api(rec, sleeps) as api:
        assert api.sharing("8e7e60e9b5bd6e0107af2ec0f8ced89f")["state"] == "disabled"
    assert len(rec.requests) == 2
    assert 6.0 <= sleeps[0] <= 7.0


def test_rate_limiting_that_never_clears_raises_rate_limited() -> None:
    rec = Recorder(*[problem(429, "RATE_LIMITED", retryable=True, retry_after="1")] * 5)
    with make_api(rec) as api, pytest.raises(RateLimited):
        api.sharing("t")
    assert len(rec.requests) == 5


def test_a_retried_write_reuses_its_idempotency_key() -> None:
    rec = Recorder(
        problem(503, "SERVICE_UNAVAILABLE", retryable=True),
        ok(fixture("detection_create")["body"], 201),
    )
    body = {
        "type": "regex",
        "displayName": "R-007 ledger v1",
        "groupId": PROJECT,
        "config": {"output_pattern": '^"?FAIL:'},
    }
    with make_api(rec) as api:
        detection = api.create_detection(body)
    keys = [r.headers["idempotency-key"] for r in rec.requests]
    assert len(keys) == 2
    assert keys[0] == keys[1]
    assert detection.type == "regex"


def test_problem_details_surface_code_and_request_id_without_retry() -> None:
    rec = Recorder(problem(400, "VALIDATION_ERROR"))
    with make_api(rec) as api, pytest.raises(ApiError) as caught:
        api.trace("t")
    assert caught.value.code == "VALIDATION_ERROR"
    assert caught.value.request_id == REQUEST_ID
    assert len(rec.requests) == 1


def test_expired_cursors_have_their_own_error() -> None:
    rec = Recorder(problem(400, "PUBLIC_API_INVALID_CURSOR"))
    with make_api(rec) as api, pytest.raises(CursorExpired):
        list(api.hits("d"))


def test_revision_conflicts_have_their_own_error() -> None:
    rec = Recorder(problem(409, "CONFLICT"))
    with make_api(rec) as api, pytest.raises(Conflict):
        api.patch_trace("t", expected_revision=0, tags=["phase:repro"])


def test_an_oauth_only_operation_without_an_oauth_token_sends_nothing() -> None:
    rec = Recorder()
    with make_api(rec) as api, pytest.raises(CredentialUnavailable, match="OAuth2"):
        api.replay_delivery(WEBHOOK, DELIVERY)
    assert rec.requests == []


def test_the_oauth_token_is_used_where_only_oauth_is_accepted() -> None:
    accepted = {
        "success": True,
        "requestId": REQUEST_ID,
        "data": {
            "accepted": True,
            "deliveryId": "29d1dbce-82b8-4467-a436-dc53ba3040c0",
            "kind": "replay",
            "sourceDeliveryId": DELIVERY,
            "webhookId": WEBHOOK,
        },
    }
    rec = Recorder(ok(accepted, 202), ok(fixture("detection_hits")["body"]))
    with make_api(rec, oauth=OAUTH) as api:
        api.replay_delivery(WEBHOOK, DELIVERY)
        list(api.hits(DETECTION))
    assert rec.requests[0].headers["authorization"] == f"Bearer {OAUTH}"
    assert rec.requests[1].headers["authorization"] == f"Bearer {TOKEN}"  # service token first


def test_request_bodies_are_checked_against_the_pin_before_sending() -> None:
    rec = Recorder()
    with make_api(rec) as api, pytest.raises(contract.ContractError, match="eventTypes"):
        api.create_webhook("x", "https://example.invalid/hook", ["alert.nope"])
    assert rec.requests == []


def test_credentials_stay_out_of_repr_and_errors() -> None:
    creds = Credentials(project_id=PROJECT, service_token=TOKEN, oauth_token=OAUTH)
    assert TOKEN not in repr(creds)
    assert OAUTH not in repr(creds)
    rec = Recorder(problem(403, "FORBIDDEN"))
    with make_api(rec) as api, pytest.raises(ApiError) as caught:
        api.project()
    assert TOKEN not in str(caught.value)
    assert TOKEN not in repr(api)


def test_credentials_from_env() -> None:
    creds = Credentials.from_env({"NEATLOGS_PROJECT_ID": PROJECT, "NEATLOGS_SERVICE_TOKEN": TOKEN})
    assert creds.service_token == TOKEN
    assert creds.oauth_token is None
    with pytest.raises(CredentialUnavailable):
        Credentials.from_env({"NEATLOGS_SERVICE_TOKEN": TOKEN})


def test_typed_adapters_read_the_recorded_shapes() -> None:
    rec = Recorder(
        ok(fixture("trace_spans")["body"]),
        ok(fixture("trace_raw_spans")["body"]),
        ok(fixture("span_payload_grader")["body"]),
        ok(fixture("webhook_deliveries")["body"]),
        ok(fixture("alert_history")["body"]),
    )
    with make_api(rec) as api:
        spans = api.spans("t")
        raw = api.raw_spans("t")
        payload = api.span_payload("t", "s", ("input", "output"))
        deliveries = api.deliveries("w")
        fires = api.alert_history(rule_id="r")
    assert {s.span_type for s in spans} == {"WORKFLOW", "LLM", "TOOL", "CHAIN"}
    assert any(s.span_name == "grader.verdict" for s in raw)
    assert payload["output"].value == '"PASS"'
    assert deliveries[0].outcome == "failed"
    assert deliveries[0].http_status is not None
    assert deliveries[0].http_status.code == 500
    assert fires
    assert fires[0].rule_name == "kintsugi spike A6 per-event"
