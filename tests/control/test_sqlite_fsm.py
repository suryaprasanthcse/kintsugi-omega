"""Reduced mode (spec §5.4): event-sourced state, leases, one writer, resume after kill -9."""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from kintsugi.control import contracts as c
from kintsugi.control import sqlite_fsm as f
from kintsugi.control.drill import LEDGER_ENV, REPO_ENV
from kintsugi.control.states import IncidentState, TransitionError
from kintsugi.control.surgeon_step import AttemptLog
from kintsugi.proof.ledger import verify
from kintsugi.sandbox import proc

IDENTITY = ("-c", "user.name=Test", "-c", "user.email=test@example.invalid")
TO_REPAIRING = [("REPRODUCED", "g0_fails_3_of_3"), ("ATTRIBUTED", "cause_report_has_suspect"),
                ("REPAIRING", "budget_positive")]  # fmt: skip


class Clock:
    def __init__(self) -> None:
        self.now = 1_791_700_000.0

    def __call__(self) -> float:
        return self.now


def test_state_is_a_fold_of_legal_transitions(tmp_path: Path) -> None:
    store = f.FsmStore(tmp_path / "omega.sqlite")
    store.open_incident("INC-1", "ledger", "phi", 12.4)
    for target, outcome in TO_REPAIRING:
        store.transition("INC-1", target, outcome)
    assert store.state("INC-1") is IncidentState.REPAIRING
    with pytest.raises(TransitionError):
        store.transition("INC-1", "RESOLVED", "sprt_accepts_h0_or_pr_only_canary_skipped")
    with pytest.raises(f.FsmError):
        store.transition("INC-1", "PARKED", "round_failed_rounds_and_budget_remain")
    store.transition("INC-1", "ESCALATED", "rounds_or_budget_exhausted_or_cheaper_treatment")
    assert store.state("INC-1") is IncidentState.ESCALATED
    assert store.open_incidents() == []


def test_leases_expire_into_abandoned_attempts(tmp_path: Path) -> None:
    clock = Clock()
    store = f.FsmStore(tmp_path / "omega.sqlite", clock=clock)
    store.open_incident("INC-1", "ledger", "phi", 1.0)
    first = store.lease("INC-1", "surgeon", "k", "worker-a", ttl_s=60)
    assert first.attempt == 1
    with pytest.raises(f.LeaseHeld):
        store.lease("INC-1", "surgeon", "k", "worker-b", ttl_s=60)
    clock.now += 30
    assert store.renew("k", "worker-a", ttl_s=60)
    clock.now += 61  # worker-a was killed: no more renewals
    assert [row["idem_key"] for row in store.expire_stale()] == ["k"]
    step = store.step("k")
    assert step is not None
    assert json.loads(step["result_json"])["abandoned"] is True
    second = store.lease("INC-1", "surgeon", "k", "worker-b", ttl_s=60)
    assert second.attempt == 2
    with pytest.raises(f.LeaseHeld):
        store.complete("k", "worker-a", {"late": True})  # the dead owner cannot finish it
    store.complete("k", "worker-b", {"commit": "abc"})
    again = store.lease("INC-1", "surgeon", "k", "worker-c", ttl_s=60)
    assert again.done
    assert again.result == {"commit": "abc"}  # DONE → skip


def test_resume_reruns_unfinished_steps_through_their_keys(tmp_path: Path) -> None:
    clock = Clock()
    store = f.FsmStore(tmp_path / "omega.sqlite", clock=clock)
    store.open_incident("INC-1", "ledger", "phi", 1.0)
    for target, outcome in TO_REPAIRING:
        store.transition("INC-1", target, outcome)
    store.lease("INC-1", "surgeon", "surgeon:INC-1:H1", "dead", ttl_s=5)
    store.lease("INC-1", "surgeon", "surgeon:INC-1:H2", "dead", ttl_s=5)
    store.complete("surgeon:INC-1:H2", "dead", {"already": "done"})
    clock.now += 10

    class Handler(f.StepHandler):
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        def execute(self, incident: str, idem_key: str, attempt: int, args: Any) -> Any:
            self.calls.append((idem_key, attempt))
            return {"attempt": attempt}

    handler = Handler()
    report = f.resume(store, {"surgeon": handler}, lambda row: {}, owner="restarted")
    assert report.incidents == ["INC-1"]
    assert [row["idem_key"] for row in report.abandoned] == ["surgeon:INC-1:H1"]
    assert handler.calls == [("surgeon:INC-1:H1", 2)]  # only the unfinished one, as attempt 2
    assert report.rerun == {"surgeon:INC-1:H1": {"attempt": 2}}


def test_the_ledger_table_is_a_verifiable_hash_chain(tmp_path: Path) -> None:
    store = f.FsmStore(tmp_path / "omega.sqlite")
    for n in range(5):
        store.ledger_append({"type": "incident", "incident": f"INC-{n}"})
    entries = store.ledger_entries()
    assert verify(entries, store.genesis).ok
    entries[2]["incident"] = "INC-tampered"
    report = verify(entries, store.genesis)
    assert not report.ok
    assert report.first_bad_seq == 3
    with pytest.raises(f.FsmError):
        store.ledger_append({"type": "x", "seq": 9})


def test_checkpoint_truncates_the_wal(tmp_path: Path) -> None:
    store = f.FsmStore(tmp_path / "omega.sqlite")
    for n in range(50):
        store.ledger_append({"type": "noise", "n": n})
    store.checkpoint()
    wal = tmp_path / "omega.sqlite-wal"
    assert not wal.exists() or wal.stat().st_size == 0


def test_the_single_writer_authenticates_and_serializes_concurrent_clients(
    tmp_path: Path,
) -> None:
    token = f.new_token()
    server = f.FsmServer(f.FsmStore(tmp_path / "omega.sqlite"), token).start()
    host, port = server.address[0], server.address[1]
    try:
        with pytest.raises(f.FsmError, match="unauthorized"):
            f.FsmClient(str(host), port, "x" * 43).call("open_incidents")
        client = f.FsmClient(str(host), port, token)
        client.call("open_incident", incident="INC-9", host="scout", signature="p", budget_usd=2.0)
        assert client.call("transition", incident="INC-9", target="REPRODUCED",
                           outcome="g0_fails_3_of_3") == "REPRODUCED"  # fmt: skip

        def append(n: int) -> None:
            mine = f.FsmClient(str(host), port, token)
            for i in range(25):
                mine.call("ledger_append", entry={"type": "load", "worker": n, "i": i})

        threads = [threading.Thread(target=append, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        entries = client.call("ledger_entries")
        assert len(entries) == 100
        assert verify(entries, "0" * 64).ok  # one writer: no forks in the chain
        client.call("lease", incident="INC-9", step="s", idem_key="k1", owner="a", ttl_s=60)
        with pytest.raises(f.LeaseHeld):
            client.call("lease", incident="INC-9", step="s", idem_key="k1", owner="b", ttl_s=60)
    finally:
        server.stop()
    with pytest.raises(ValueError, match="256 bits"):
        f.FsmServer(f.FsmStore(":memory:"), "short")


# --------------------------------------------------------------------------- the kill -9 drill


def git(repo: Path, *args: str) -> str:
    return proc.run(["git", "-C", str(repo), *IDENTITY, *args], timeout_s=60,
                    check=True).stdout.strip()  # fmt: skip


def start_writer(db: Path, token_file: Path) -> tuple[proc.Proc, int, dict[str, Any]]:
    writer = proc.start([sys.executable, "-m", "kintsugi.control.sqlite_fsm", "serve", "--db",
                         str(db), "--token-file", str(token_file)])  # fmt: skip
    line = next(writer.lines())
    _, port, recovered = line.split(" ", 2)
    return writer, int(port), json.loads(recovered)


def run_step(port: int, token_file: Path, card: c.Card, env: dict[str, str]) -> proc.Proc:
    return proc.start([sys.executable, "-m", "kintsugi.control.sqlite_fsm", "run-step", "--port",
                       str(port), "--token-file", str(token_file), "--card",
                       json.dumps(asdict(card)), "--idem", f"surgeon:{card.incident_id}:H1",
                       "--ttl", "2"], extra_env=env)  # fmt: skip


@pytest.mark.parametrize("mode", ["adopt", "cleanup"])
def test_reduced_mode_survives_kill9_of_the_whole_stack(tmp_path: Path, mode: str) -> None:
    if proc.which("git") is None:
        pytest.skip("git is not installed")
    repo = tmp_path / "host"
    proc.run(["git", "init", "-q", "-b", "main", str(repo)], timeout_s=30, check=True)
    (repo / "reconcile.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    db, token_file, attempts = tmp_path / "omega.sqlite", tmp_path / "fsm.token", tmp_path / "a.db"
    f.write_token(token_file)
    env = {REPO_ENV: str(repo), LEDGER_ENV: str(attempts)}
    card = c.Card("INC-L2", "H1", 0, f"drill:{mode}", "main")

    writer, port, _ = start_writer(db, token_file)
    client = f.FsmClient("127.0.0.1", port, f.read_token(token_file))
    client.call("open_incident", incident="INC-L2", host="ledger", signature="phi", budget_usd=9.0)
    for target, outcome in TO_REPAIRING:
        client.call("transition", incident="INC-L2", target=target, outcome=outcome)
    executor = run_step(port, token_file, card, env)
    log, worktree = AttemptLog(attempts), repo / "wt" / "INC-L2" / "H1"
    deadline = time.monotonic() + 60
    while not ((mode == "adopt" and len(git(repo, "log", "--oneline", "--all").splitlines()) > 1)
               or (mode == "cleanup" and (worktree / "scratch.txt").exists())):  # fmt: skip
        assert time.monotonic() < deadline, "attempt 1 never reached the drill state"
        time.sleep(0.2)
    proc.kill_tree(executor.pid)  # the crew worker and its session…
    proc.kill_tree(writer.pid)  # …and the single writer: an unclean start follows
    assert executor.wait(timeout_s=10) is not None
    assert writer.wait(timeout_s=10) is not None
    time.sleep(2.5)  # the dead executor's lease (2 s) expires

    writer2, port2, recovered = start_writer(db, token_file)
    try:
        assert recovered["incidents"] == {"INC-L2": "REPAIRING"}  # WAL recovery + the fold
        assert recovered["abandoned"] == ["surgeon:INC-L2:H1"]
        second = run_step(port2, token_file, card, env)  # the Watchdog's restart
        output = [line for line in second.lines() if line.startswith("{")]
        assert second.wait(timeout_s=60) == 0
        result = json.loads(output[-1])
        client2 = f.FsmClient("127.0.0.1", port2, f.read_token(token_file))
        step = client2.call("step", idem_key="surgeon:INC-L2:H1")
        assert (step["status"], step["attempt"]) == ("DONE", 2)
        assert client2.call("state", incident="INC-L2") == "REPAIRING"
    finally:
        writer2.kill_tree()
    states = [state for _, state, _, _ in log.history("INC-L2", "H1")]
    if mode == "adopt":
        assert result["adopted"] is True
        assert states == ["adopted"]
    else:
        assert result["adopted"] is False
        assert states == ["abandoned", "committed"]
        assert result["nonce"] != log.nonces("INC-L2", "H1")[0]
        assert not (worktree / "scratch.txt").exists()
