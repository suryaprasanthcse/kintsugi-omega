from __future__ import annotations

import concurrent.futures as cf
import json
from datetime import UTC, datetime
from typing import Any, ClassVar

import anthropic
import httpx
import pytest

from kintsugi.econ.governor import BudgetExceeded
from kintsugi.econ.prices import usage_cost
from kintsugi.sandbox import llm_proxy as lp
from kintsugi.sandbox.cassette import Player, Recorder
from kintsugi.sandbox.httpserve import TASK_HEADER, HttpRequest, HttpResponse, Server

TOKEN = "kintsugi-proxy-token-ledger"
HAIKU = "claude-haiku-5-5"


def _ledger(cap: float = 20.0, monthly: float = 100.0) -> lp.SpendLedger:
    return lp.SpendLedger(":memory:", daily_cap_usd=cap,
                          limits={"host:ledger": lp.AccountLimits(monthly)})  # fmt: skip


def _request(body: dict[str, Any], *, token: str = TOKEN, path: str = "/h/ledger/v1/messages",
             task: str = "T1") -> HttpRequest:  # fmt: skip
    raw = json.dumps(body).encode()
    return HttpRequest("POST", path, {}, {"x-api-key": token, TASK_HEADER: task}, raw)


def _body(**extra: Any) -> dict[str, Any]:
    return {"model": HAIKU, "max_tokens": 512,
            "messages": [{"role": "user", "content": "reconcile INV-1"}], **extra}  # fmt: skip


class FakeBillable:
    """A billable upstream with a known usage per call."""

    name = "fake"
    billable = True
    usage: ClassVar[dict[str, Any]] = {
        "input_tokens": 1200,
        "output_tokens": 300,
        "cache_read_input_tokens": 800,
        "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 0},
    }

    def messages(self, host: str, body: dict[str, Any], request: HttpRequest) -> lp.Reply:
        message = {"id": "m", "type": "message", "role": "assistant", "model": body["model"],
                   "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                   "usage": self.usage}  # fmt: skip
        if body.get("stream"):
            return lp.Reply(200, {}, events=lp.sse_from_message(message))
        return lp.Reply(200, {}, message=message)

    def count_tokens(self, host: str, body: dict[str, Any], request: HttpRequest) -> lp.Reply:
        return lp.Reply(200, {}, message={"input_tokens": 7})


def _responder(body: dict[str, Any]) -> dict[str, Any]:
    text = body["messages"][-1]["content"]
    return {"content": [{"type": "text", "text": f"echo: {text}"}]}


# ---- ledger -----------------------------------------------------------------------------------


def test_reserve_settle_and_release() -> None:
    ledger = _ledger()
    rid = ledger.reserve("host:ledger", 1.0)
    assert ledger.outstanding_reservations() == 1
    ledger.settle(rid, account="host:ledger", model=HAIKU, cost_usd=0.25, usage={},
                  upstream="fake", billable=True)  # fmt: skip
    assert ledger.outstanding_reservations() == 0
    assert ledger.spent(account="host:ledger") == pytest.approx(0.25)
    ledger.release(ledger.reserve("host:ledger", 3.0))
    assert ledger.outstanding_reservations() == 0


def test_caps_refuse_a_charge_whole() -> None:
    ledger = _ledger(cap=1.0, monthly=10.0)
    ledger.reserve("host:ledger", 0.6)
    with pytest.raises(BudgetExceeded, match="daily cap"):
        ledger.reserve("host:ledger", 0.5)
    monthly = _ledger(cap=100.0, monthly=1.0)
    with pytest.raises(BudgetExceeded, match="monthly budget"):
        monthly.reserve("host:ledger", 1.5)
    with pytest.raises(BudgetExceeded, match="no budget"):
        monthly.reserve("host:scout", 0.01)


def test_concurrent_reservations_never_overshoot_the_cap() -> None:
    ledger = _ledger(cap=20.0)

    def attempt(_: int) -> bool:
        try:
            ledger.reserve("host:ledger", 0.5)
            return True
        except BudgetExceeded:
            return False

    with cf.ThreadPoolExecutor(max_workers=16) as pool:
        granted = sum(pool.map(attempt, range(60)))
    assert granted == 40


def test_the_cap_is_per_utc_day() -> None:
    now = [datetime(2026, 10, 11, 23, 59, tzinfo=UTC)]
    limits = {"host:ledger": lp.AccountLimits(100.0)}
    ledger = lp.SpendLedger(":memory:", daily_cap_usd=1.0, limits=limits, clock=lambda: now[0])
    rid = ledger.reserve("host:ledger", 0.9)
    ledger.settle(rid, account="host:ledger", model=HAIKU, cost_usd=0.9, usage={},
                  upstream="fake", billable=True)  # fmt: skip
    with pytest.raises(BudgetExceeded):
        ledger.reserve("host:ledger", 0.2)
    now[0] = datetime(2026, 10, 12, 0, 1, tzinfo=UTC)
    ledger.reserve("host:ledger", 0.2)


# ---- proxy ------------------------------------------------------------------------------------


def test_metered_cost_equals_list_price_cost_over_many_calls() -> None:
    ledger = _ledger()
    proxy = lp.LlmProxy(FakeBillable(), ledger, {"ledger": TOKEN})
    for _ in range(25):
        response = proxy(_request(_body()))
        assert response.status == 200
        assert float(response.headers[lp.COST_HEADER]) > 0
    expected = 25 * usage_cost(HAIKU, FakeBillable.usage)
    assert ledger.spent(account="host:ledger") == pytest.approx(expected, rel=0.01)
    assert ledger.outstanding_reservations() == 0
    assert {c["task"] for c in ledger.charges()} == {"T1"}


def test_budget_exhaustion_is_a_402_and_nothing_is_forwarded() -> None:
    calls: list[int] = []

    class Counting(FakeBillable):
        def messages(self, host: str, body: dict[str, Any], request: HttpRequest) -> lp.Reply:
            calls.append(1)
            return super().messages(host, body, request)

    proxy = lp.LlmProxy(Counting(), _ledger(cap=0.0005), {"ledger": TOKEN})
    response = proxy(_request(_body(max_tokens=4000)))
    assert response.status == 402
    assert response.json()["error"]["type"] == "budget_exceeded"
    assert calls == []


def test_auth_routes_and_models_are_checked() -> None:
    proxy = lp.LlmProxy(FakeBillable(), _ledger(), {"ledger": TOKEN})
    assert proxy(_request(_body(), token="wrong")).status == 401
    assert proxy(_request(_body(), path="/h/scout/v1/messages")).status == 401
    assert proxy(_request(_body(), path="/h/ledger/v1/complete")).status == 404
    assert proxy(_request(_body(model="gpt-5"))).status == 400
    assert proxy(_request(_body(), path="/h/ledger/v1/messages/count_tokens")).json() == {
        "input_tokens": 7
    }


def test_streaming_is_relayed_and_charged_from_message_delta() -> None:
    ledger = _ledger()
    proxy = lp.LlmProxy(FakeBillable(), ledger, {"ledger": TOKEN})
    response = proxy(_request(_body(stream=True)))
    assert response.stream is not None
    raw = b"".join(response.stream)
    assert b"event: message_stop" in raw
    expected = usage_cost(HAIKU, FakeBillable.usage)
    assert ledger.spent(account="host:ledger") == pytest.approx(expected)


def test_sse_usage_survives_arbitrary_chunk_boundaries() -> None:
    message = {
        "id": "m",
        "model": HAIKU,
        "content": [{"type": "text", "text": "hi"}],
        "usage": {"input_tokens": 50, "output_tokens": 9},
        "stop_reason": "end_turn",
    }
    stream = b"".join(lp.sse_from_message(message))
    meter = lp.SseUsage()
    for i in range(0, len(stream), 7):
        meter.feed(stream[i : i + 7])
    assert meter.usage["input_tokens"] == 50
    assert meter.usage["output_tokens"] == 9


def test_offline_upstream_is_metered_but_never_consumes_the_cap() -> None:
    ledger = _ledger(cap=0.000001)
    proxy = lp.LlmProxy(lp.OfflineUpstream({"ledger": _responder}), ledger, {"ledger": TOKEN})
    first = proxy(_request(_body())).json()
    second = proxy(_request(_body())).json()
    assert first == second  # deterministic
    assert first["content"][0]["text"] == "echo: reconcile INV-1"
    assert ledger.spent(account="host:ledger", billable=False) > 0
    assert ledger.spent(account="host:ledger") == 0
    unknown = lp.LlmProxy(lp.OfflineUpstream({}), ledger, {"ledger": TOKEN})
    assert unknown(_request(_body())).status == 501


def test_record_then_replay_returns_the_same_message() -> None:
    recorder = Recorder("llm")
    live = lp.LlmProxy(lp.OfflineUpstream({"ledger": _responder}), _ledger(), {"ledger": TOKEN},
                       recorder=recorder)  # fmt: skip
    recorded = live(_request(_body(), task="T9")).json()
    replay = lp.LlmProxy(lp.CassetteUpstream(Player(recorder.cassette())), _ledger(),
                         {"ledger": TOKEN})  # fmt: skip
    assert replay(_request(_body(), task="T9")).json() == recorded
    assert replay(_request(_body(), task="T9")).status == 502  # a second call was never recorded


def test_the_anthropic_sdk_works_through_the_proxy_including_streaming() -> None:
    ledger = _ledger()
    proxy = lp.LlmProxy(lp.OfflineUpstream({"ledger": _responder}), ledger, {"ledger": TOKEN})
    with Server(proxy) as server:
        base = f"{server.url}/h/ledger"
        client = anthropic.Anthropic(base_url=base, api_key=TOKEN, max_retries=0)
        ask = [{"role": "user", "content": "INV-7"}]
        message = client.messages.create(model=HAIKU, max_tokens=64, messages=ask)  # type: ignore[arg-type]
        assert message.content[0].type == "text"
        assert message.content[0].text == "echo: INV-7"
        ask = [{"role": "user", "content": "INV-8"}]
        with client.messages.stream(model=HAIKU, max_tokens=64, messages=ask) as stream:  # type: ignore[arg-type]
            final = stream.get_final_message()
        assert final.content[0].type == "text"
        assert final.content[0].text == "echo: INV-8"
        with pytest.raises(anthropic.AuthenticationError):
            anthropic.Anthropic(base_url=f"{server.url}/h/ledger", api_key="nope",
                                max_retries=0).messages.create(
                model=HAIKU, max_tokens=8, messages=[{"role": "user", "content": "x"}])  # fmt: skip
    assert len(ledger.charges()) == 2


def test_the_anthropic_upstream_injects_the_key_and_never_forwards_the_proxy_token() -> None:
    seen: list[dict[str, str]] = []

    def fake_api(request: HttpRequest) -> HttpResponse:
        seen.append(dict(request.headers))
        body = request.json()
        message = {"id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
                   "content": [{"type": "text", "text": "live"}], "stop_reason": "end_turn",
                   "usage": {"input_tokens": 10, "output_tokens": 2}}  # fmt: skip
        if body.get("stream"):
            return HttpResponse(200, {"content-type": "text/event-stream"},
                                stream=lp.sse_from_message(message))  # fmt: skip
        return HttpResponse.of_json(200, message)

    real_key = "sk-ant-api03-" + "x" * 20  # fake, assembled at runtime
    with Server(fake_api) as api:
        upstream = lp.AnthropicUpstream(real_key, base_url=api.url)
        assert "x" * 20 not in repr(upstream)
        ledger = _ledger()
        proxy = lp.LlmProxy(upstream, ledger, {"ledger": TOKEN})
        assert proxy(_request(_body())).json()["content"][0]["text"] == "live"
        streamed = proxy(_request(_body(stream=True)))
        assert streamed.stream is not None
        assert b"message_stop" in b"".join(streamed.stream)
    assert all(h["x-api-key"] == real_key for h in seen)
    assert all(TOKEN not in json.dumps(h) for h in seen)
    assert ledger.spent(account="host:ledger") == pytest.approx(
        2 * usage_cost(HAIKU, {"input_tokens": 10, "output_tokens": 2})
    )


def test_upstream_errors_release_the_reservation() -> None:
    class Failing(FakeBillable):
        def messages(self, host: str, body: dict[str, Any], request: HttpRequest) -> lp.Reply:
            return lp.Reply(529, {}, error=b'{"type":"error","error":{"type":"overloaded_error"}}')

    ledger = _ledger()
    response = lp.LlmProxy(Failing(), ledger, {"ledger": TOKEN})(_request(_body()))
    assert response.status == 529
    assert ledger.outstanding_reservations() == 0
    assert ledger.charges() == []


def test_proxy_over_http_reports_cost_header() -> None:
    proxy = lp.LlmProxy(FakeBillable(), _ledger(), {"ledger": TOKEN})
    with Server(proxy) as server:
        reply = httpx.post(f"{server.url}/h/ledger/v1/messages", json=_body(),
                           headers={"x-api-key": TOKEN})  # fmt: skip
    assert reply.status_code == 200
    expected = usage_cost(HAIKU, FakeBillable.usage)
    assert float(reply.headers[lp.COST_HEADER]) == pytest.approx(expected)
