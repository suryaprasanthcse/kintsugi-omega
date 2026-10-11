from __future__ import annotations

import concurrent.futures as cf
import random
from typing import Any

import httpx
import pytest

from kintsugi.sandbox import chaos_proxy as chaos
from kintsugi.sandbox.cassette import Player, Recorder
from kintsugi.sandbox.httpserve import TASK_HEADER, HttpRequest, HttpResponse, Server


def _fx(request: HttpRequest) -> HttpResponse:
    return HttpResponse.of_json(200, {"base": request.query.get("base"), "rate": 1.08})


class SideEffects:
    """A side-effecting mock upstream that counts real calls."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.calls += 1
        return HttpResponse.of_json(200, {"ok": True})


def _call(proxy: chaos.ChaosProxy, task: str, path: str = "/fx/rate", **query: str) -> int:
    return proxy(HttpRequest("GET", path, query, {TASK_HEADER: task})).status


def _always(fault: chaos.FaultKind, **params: Any) -> chaos.Profile:
    return chaos.Profile("t", 1, (chaos.Rule("*", fault, 1.0, "*", params),))


def test_the_background_profile_is_the_disclosed_one() -> None:
    profile = chaos.background_profile()
    assert profile.disclosed
    fx = next(r for r in profile.rules if r.service == "fx")
    assert (fx.fault, fx.rate, fx.params["status"], fx.path) == ("status", 0.08, 429, "/rate")


def test_draws_are_deterministic_uniform_and_seed_dependent() -> None:
    draws = [chaos.unit(7, "fx", "T", "k", n) for n in range(5000)]
    assert draws == [chaos.unit(7, "fx", "T", "k", n) for n in range(5000)]
    assert all(0.0 <= u < 1.0 for u in draws)
    assert abs(sum(u < 0.08 for u in draws) / 5000 - 0.08) < 0.012
    assert draws[:50] != [chaos.unit(8, "fx", "T", "k", n) for n in range(50)]


def test_identical_faults_per_seed_under_any_thread_interleaving() -> None:
    profile = chaos.background_profile().with_seed(2)
    calls = [(f"T{t}", cur) for t in range(30) for cur in ("EUR", "GBP", "EUR", "JPY")]

    def run(order_seed: int) -> set[tuple[str, int, str, int]]:
        proxy = chaos.ChaosProxy({"fx": _fx}, profile, sleep=lambda s: None)
        shuffled = calls[:]
        random.Random(order_seed).shuffle(shuffled)
        with cf.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda c: _call(proxy, c[0], base=c[1]), shuffled))
        return {(r["task"], r["occurrence"], r["fired"], r["status"]) for r in proxy.log}

    first = run(1)
    assert first == run(2) == run(3)
    assert any(status == 429 for *_, status in first)  # 120 calls at 8%: some fire
    other_seed = chaos.ChaosProxy({"fx": _fx}, profile.with_seed(3), sleep=lambda s: None)
    for task, cur in calls:
        _call(other_seed, task, base=cur)
    assert {(r["task"], r["occurrence"], r["fired"], r["status"]) for r in other_seed.log} != first


def test_an_injected_status_never_reaches_a_side_effecting_upstream() -> None:
    upstream = SideEffects()
    proxy = chaos.ChaosProxy({"github": upstream}, _always("status", status=429))
    assert _call(proxy, "T1", "/github/issues/4/labels") == 429
    assert upstream.calls == 0
    clean = chaos.ChaosProxy({"github": upstream}, chaos.Profile("none", 1, ()))
    assert _call(clean, "T1", "/github/issues/4/labels") == 200
    assert upstream.calls == 1


def test_schema_drift_and_partial_responses_reshape_json() -> None:
    drift = chaos.ChaosProxy({"fx": _fx}, _always("schema_drift", rename={"rate": "fx_rate"}))
    body = drift(HttpRequest("GET", "/fx/rate", {"base": "EUR"}, {})).json()
    assert body == {"base": "EUR", "fx_rate": 1.08}

    def listing(request: HttpRequest) -> HttpResponse:
        return HttpResponse.of_json(200, {"items": list(range(10))})

    partial = chaos.ChaosProxy({"docs": listing}, _always("partial", field="items", keep=0.3))
    assert partial(HttpRequest("GET", "/docs/list", {}, {})).json() == {"items": [0, 1, 2]}


def test_latency_is_bounded_and_reported_in_a_header() -> None:
    slept: list[float] = []
    proxy = chaos.ChaosProxy({"fx": _fx}, _always("latency", latency_ms=[10, 20]),
                             sleep=slept.append)  # fmt: skip
    response = proxy(HttpRequest("GET", "/fx/rate", {}, {TASK_HEADER: "T"}))
    assert 0.010 <= slept[0] <= 0.020
    assert response.headers[chaos.CHAOS_HEADER] == "latency#0"


def test_unknown_service_is_a_404() -> None:
    proxy = chaos.ChaosProxy({"fx": _fx}, chaos.Profile("none", 1, ()))
    assert _call(proxy, "T", "/evil/exfiltrate") == 404


def test_record_then_replay_reproduces_faults_without_the_upstream() -> None:
    recorder = Recorder("fx-429")
    profile = chaos.background_profile().with_seed(5)
    live = chaos.ChaosProxy({"fx": _fx}, profile, recorder=recorder, sleep=lambda s: None)
    statuses = [_call(live, f"T{n % 7}", base="EUR") for n in range(70)]
    assert 429 in statuses

    def dead(request: HttpRequest) -> HttpResponse:
        raise AssertionError("replay must not call the upstream")

    replay = chaos.ChaosProxy({"fx": dead}, profile, player=Player(recorder.cassette()))
    assert [_call(replay, f"T{n % 7}", base="EUR") for n in range(70)] == statuses


def test_record_and_replay_are_exclusive() -> None:
    with pytest.raises(ValueError, match="not both"):
        chaos.ChaosProxy({}, chaos.Profile("n", 1, ()), recorder=Recorder("a"),
                         player=Player(Recorder("b").cassette()))  # fmt: skip


def test_the_proxy_serves_over_http_and_forwards_to_a_url_upstream() -> None:
    with Server(_fx) as upstream:
        proxy = chaos.ChaosProxy({"fx": upstream.url}, chaos.Profile("none", 1, ()))
        with Server(proxy) as front:
            reply = httpx.get(f"{front.url}/fx/rate", params={"base": "EUR"},
                              headers={TASK_HEADER: "T1"})  # fmt: skip
        proxy.close()
    assert reply.status_code == 200
    assert reply.json() == {"base": "EUR", "rate": 1.08}


def test_bad_profiles_are_rejected(tmp_path: Any) -> None:
    path = tmp_path / "p.yaml"
    path.write_text("name: x\nrules:\n  - {service: fx, fault: explode, rate: 0.1}\n")
    with pytest.raises(ValueError, match="fault kind"):
        chaos.load_profile(path)
    path.write_text("name: x\nrules:\n  - {service: fx, fault: status, rate: 1.5}\n")
    with pytest.raises(ValueError, match="rate"):
        chaos.load_profile(path)
