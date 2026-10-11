"""The Watchdog (spec §5.5) and the senses → control-plane forwarder (§5.6)."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from kintsugi.control import contracts as c
from kintsugi.control import watchdog as w
from kintsugi.control.hits import Forwarder, supervisor_deliver
from kintsugi.control.workflows import FleetSupervisor
from kintsugi.senses.ingress import IngressConfig, create_app, sign
from kintsugi.senses.store import HitEvent, SenseStore
from tests.helpers.control import Scenario, crew_for, temporal

TOKEN = "t" * 43
EXIT = [sys.executable, "-c", "import sys; sys.exit(3)"]
SLEEP = [sys.executable, "-c", "import time; time.sleep(120)"]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def wait_exit(pool: w.Pool, timeout: float = 15.0) -> None:
    assert pool.process is not None
    assert pool.process.wait(timeout_s=timeout) is not None


def test_a_crashing_pool_is_restarted_five_times_in_ten_minutes_then_paged(tmp_path: Path) -> None:
    clock = Clock()
    pager = w.Pager(tmp_path / "pages.jsonl")
    sup = w.Supervisor(pager, clock=clock)
    pool = w.Pool("surgeon", EXIT, log=tmp_path / "surgeon.log")
    sup.add(pool)
    for _ in range(5):
        wait_exit(pool)
        clock.now += 10
        assert sup.tick() == ["surgeon"]
    wait_exit(pool)
    assert sup.tick() == []  # the sixth exit inside the window: page, stay down
    assert pool.paged
    assert pool.starts == 6
    assert [p["reason"] for p in pager.pages] == ["restart budget exhausted"]
    record = json.loads((tmp_path / "pages.jsonl").read_text("utf-8").splitlines()[0])
    assert record["pool"] == "surgeon"


def test_restarts_outside_the_window_do_not_count(tmp_path: Path) -> None:
    clock = Clock()
    sup = w.Supervisor(w.Pager(tmp_path / "pages.jsonl"), clock=clock)
    pool = w.Pool("crew", EXIT)
    sup.add(pool)
    for _ in range(8):
        wait_exit(pool)
        clock.now += 200  # one restart every 200 s: never 5 inside 600 s
        assert sup.tick() == ["crew"]
    assert not pool.paged


def test_the_chaos_api_kills_a_pool_and_the_supervisor_brings_it_back(tmp_path: Path) -> None:
    pager = w.Pager(tmp_path / "pages.jsonl")
    sup = w.Supervisor(pager)
    sup.add(w.Pool("surgeon", SLEEP))
    sup.add(w.Pool("senses", SLEEP))
    dog = w.Watchdog(sup, pager, chaos_log=tmp_path / "chaos.jsonl")
    api = TestClient(w.chaos_app(dog, TOKEN))
    auth = {"Authorization": f"Bearer {TOKEN}"}
    try:
        assert api.post("/chaos/kill", json={"pool": "surgeon"}).status_code == 401
        no_seal = api.post("/chaos/kill", json={"pool": "surgeon"}, headers=auth)
        assert no_seal.status_code == 400
        target = api.post("/chaos/kill", json={"pool": "watchdog", "sabotage_id": "S-18"},
                          headers=auth)  # fmt: skip
        assert target.status_code == 403
        unknown = api.post("/chaos/kill", json={"pool": "nope", "sabotage_id": "S-18"},
                           headers=auth)  # fmt: skip
        assert unknown.status_code == 404
        before = sup.pid("surgeon")
        senses = sup.pid("senses")
        killed = api.post("/chaos/kill", json={"pool": "surgeon", "sabotage_id": "S-18"},
                          headers=auth)  # fmt: skip
        assert killed.json() == {"killed": "surgeon", "pid": before}
        assert sup.tick() == ["surgeon"]  # restarted on the next tick (§6.6: t+5 s)
        assert sup.pid("surgeon") != before
        assert sup.pid("senses") == senses  # the senses pool was not targeted
        log = [json.loads(line) for line in (tmp_path / "chaos.jsonl").read_text().splitlines()]
        assert [(e["op"], e["sabotage_id"], e["pool"]) for e in log] == [("kill", "S-18",
                                                                           "surgeon")]  # fmt: skip
    finally:
        sup.stop()
    with pytest.raises(ValueError, match="256 bits"):
        w.chaos_app(dog, "short")


def test_the_chaos_api_drives_an_ingress_outage(tmp_path: Path) -> None:
    switches: list[bool] = []
    pager = w.Pager(tmp_path / "p.jsonl")
    dog = w.Watchdog(w.Supervisor(pager), pager, ingress_toggle=switches.append,
                     chaos_log=tmp_path / "chaos.jsonl")  # fmt: skip
    api = TestClient(w.chaos_app(dog, TOKEN))
    auth = {"Authorization": f"Bearer {TOKEN}"}
    response = api.post("/chaos/ingress", json={"available": False, "duration_s": 0.3,
                                                "sabotage_id": "S-18"}, headers=auth)  # fmt: skip
    assert response.status_code == 200
    assert switches == [False]
    time.sleep(0.8)
    assert switches == [False, True]  # back on after the outage
    assert "ingress" in (tmp_path / "chaos.jsonl").read_text()


def test_the_ingress_admin_switch_needs_its_token(tmp_path: Path) -> None:
    store = SenseStore(tmp_path / "s.sqlite")
    config = IngressConfig(path_secret="p" * 43, signing_secret="s" * 43,
                           evidence_dir=tmp_path / "ev", admin_token="a" * 43)  # fmt: skip
    client = TestClient(create_app(config, store))
    body = json.dumps({"id": "alert:1", "type": "alert.per_event_match", "data": {}}).encode()
    headers = {"x-neatlogs-signature": sign("s" * 43, body)}
    wrong = client.post("/admin/availability", json={"available": False},
                        headers={"Authorization": "Bearer " + "b" * 43})  # fmt: skip
    assert wrong.status_code == 401
    off = client.post("/admin/availability", json={"available": False},
                      headers={"Authorization": "Bearer " + "a" * 43})  # fmt: skip
    assert off.json() == {"available": False}
    assert client.post(f"/hook/{'p' * 43}", content=body, headers=headers).status_code == 503
    client.post("/admin/availability", json={"available": True},
                headers={"Authorization": "Bearer " + "a" * 43})  # fmt: skip
    assert client.post(f"/hook/{'p' * 43}", content=body, headers=headers).status_code == 200


def test_health_checks_page_once_per_failure(tmp_path: Path) -> None:
    pager = w.Pager(tmp_path / "pages.jsonl")
    store = SenseStore(tmp_path / "s.sqlite")
    state = {"ok": False}
    dog = w.Watchdog(w.Supervisor(pager), pager, checks={
        "flaky": lambda: w.Health(state["ok"], "flaky"),
        "queue": w.queue_check(store, max_pending=1),
        "cmd-ok": w.command_check([sys.executable, "-c", "print('{}')"]),
        "cmd-bad": w.command_check([sys.executable, "-c", "import sys; sys.exit(1)"]),
    })  # fmt: skip
    first = dog.run_checks()
    assert {k: v.ok for k, v in first.items()} == {"flaky": False, "queue": True, "cmd-ok": True,
                                                   "cmd-bad": False}  # fmt: skip
    dog.run_checks()
    assert sorted(p["check"] for p in pager.pages) == ["cmd-bad", "flaky"]  # not paged twice
    state["ok"] = True
    for n in range(3):
        store.claim_hit(HitEvent(f"k{n}", "d", f"t{n}", "s", "poller"))
    second = dog.run_checks()
    assert second["flaky"].ok
    assert not second["queue"].ok  # 3 waiting > 1


def test_waking_runs_every_check_now_and_pages(tmp_path: Path) -> None:
    pager = w.Pager(tmp_path / "pages.jsonl")
    healthy = w.Watchdog(w.Supervisor(pager), pager, checks={"ok": lambda: w.Health(True)})
    assert healthy.wake("ingestion silent: alert:7")["ok"].ok
    assert pager.pages[-1]["cause"] == "ingestion silent: alert:7"
    sick = w.Watchdog(w.Supervisor(pager), pager, checks={"temporal": lambda: w.Health(False)})
    assert not sick.wake("ingestion silent: alert:8")["temporal"].ok
    assert pager.pages[-1] == {**pager.pages[-1], "reason": "health check failing",
                               "check": "temporal"}  # fmt: skip


def test_the_dead_man_pages_on_a_stale_or_missing_heartbeat(tmp_path: Path) -> None:
    pager = w.Pager(tmp_path / "pages.jsonl")
    dog = w.Watchdog(w.Supervisor(pager), pager, heartbeat_path=tmp_path / "hb")
    assert not w.deadman(tmp_path / "hb", 60, pager)
    dog.beat()
    assert w.deadman(tmp_path / "hb", 60, pager)
    assert not w.deadman(tmp_path / "hb", 60, pager, now=time.time() + 3600)
    reasons = [p["reason"] for p in pager.pages]
    assert reasons == ["dead man: no watchdog heartbeat", "dead man: watchdog heartbeat is stale"]
    assert w.main(["deadman", "--heartbeat", str(tmp_path / "hb"), "--max-age", "60",
                   "--pages", str(tmp_path / "pages2.jsonl")]) == 0  # fmt: skip


# --------------------------------------------------------------------------- forwarding


def test_the_forwarder_delivers_hits_at_least_once_and_wakes_the_watchdog(tmp_path: Path) -> None:
    store = SenseStore(tmp_path / "s.sqlite")
    for n in range(3):
        store.claim_hit(HitEvent(f"k{n}", "d", f"t{n}", "s", "poller", receptor="R-007",
                                 host="ledger"))  # fmt: skip
    store.enqueue_alert({"event_id": "alert:9", "action": "wake_watchdog"})
    delivered: list[c.Hit] = []
    woken: list[str] = []
    fail_once = {"left": 1}

    async def deliver(hit: c.Hit) -> None:
        if hit.hit_key == "k1" and fail_once["left"]:
            fail_once["left"] -= 1
            raise ConnectionError("temporal unavailable")
        delivered.append(hit)

    forwarder = Forwarder(store, deliver, wake=woken.append)
    assert asyncio.run(forwarder.drain()) == 3
    assert [h.hit_key for h in delivered] == ["k0", "k2"]
    assert forwarder.released == 1
    assert woken == ["ingestion silent: alert:9"]
    assert asyncio.run(forwarder.drain()) == 1  # the released item, retried
    assert [h.hit_key for h in delivered] == ["k0", "k2", "k1"]
    assert delivered[0].receptor == "R-007"
    assert store.last_taken_at() is not None


def test_forwarded_hits_reach_the_fleet_supervisor(tmp_path: Path) -> None:
    async def scenario() -> dict[str, Any]:
        store = SenseStore(tmp_path / "s.sqlite")
        for n in range(4):
            store.claim_hit(HitEvent(f"k{n}", "d", f"t{n}", "s", "webhook", receptor="R-007",
                                     host="ledger"))  # fmt: skip
        crew = crew_for(Scenario(route="drop"))
        config = c.ControlConfig()
        async with temporal(crew) as (_env, client):
            sup = await client.start_workflow(
                FleetSupervisor.run, args=[config, None], id=config.supervisor_id,
                task_queue="kintsugi-crew",
            )  # fmt: skip
            forwarder = Forwarder(store, supervisor_deliver(client, config.supervisor_id))
            await forwarder.drain()
            deadline = time.monotonic() + 20
            while (await sup.query(FleetSupervisor.status))["events"] < 4:
                assert time.monotonic() < deadline
                await asyncio.sleep(0.05)
            status = await sup.query(FleetSupervisor.status)
            await sup.terminate("test over")
        return status

    status = asyncio.run(scenario())
    assert status["events"] == 4
