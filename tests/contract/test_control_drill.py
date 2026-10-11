"""The L2 kill -9 drill on a real Temporal server (spec §6.6; run with KINTSUGI_CONTRACT=1).

A worker process runs the surgeon activity; its stand-in session reaches the drill's state
(committed, or dirty without a commit); the test kills the worker's process tree; a new worker
starts. The server's heartbeat timeout fails attempt 1 and the retry policy starts attempt 2,
which adopts the commit by its nonce or cleans up and commits under a new nonce. The evidence is
the server's own event history (T8): attempt 2 started with a HEARTBEAT timeout as its last
failure. Needs the Temporal CLI dev server (fetched by the SDK on first use, ruling D6) and git.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from temporalio.api.enums.v1 import EventType, TimeoutType
from temporalio.testing import WorkflowEnvironment

from kintsugi.control import contracts as c
from kintsugi.control.activities import CREW
from kintsugi.control.drill import DRILL_CONFIG, DRILL_TIMEOUT, LEDGER_ENV, REPO_ENV, SurgeonDrill
from kintsugi.control.surgeon_step import AttemptLog
from kintsugi.sandbox import proc

pytestmark = pytest.mark.contract
IDENTITY = ("-c", "user.name=Test", "-c", "user.email=test@example.invalid")


def git(repo: Path, *args: str) -> str:
    return proc.run(["git", "-C", str(repo), *IDENTITY, *args], timeout_s=60,
                    check=True).stdout.strip()  # fmt: skip


def make_repo(tmp: Path) -> Path:
    repo = tmp / "host"
    proc.run(["git", "init", "-q", "-b", "main", str(repo)], timeout_s=30, check=True)
    (repo / "reconcile.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return repo


async def drill(mode: str, tmp: Path) -> tuple[c.SurgeonOutcome, Any, Path, AttemptLog, str]:
    repo = make_repo(tmp)
    attempts = tmp / "attempts.sqlite"
    log = AttemptLog(attempts)
    env_vars = {REPO_ENV: str(repo), LEDGER_ENV: str(attempts)}
    async with await WorkflowEnvironment.start_local() as env:
        target = env.client.service_client.config.target_host

        def worker(name: str) -> proc.Proc:
            argv = [sys.executable, "-m", "kintsugi.control.worker", "--target", target,
                    "--pool", "crew", "--pool", "surgeon", "--crew", "kintsugi.control.drill:crew",
                    "--workflows", "kintsugi.control.drill:WORKFLOWS",
                    "--ledger", str(tmp / "steps.sqlite"), "--heartbeat-interval", "0.5",
                    "--log", str(tmp / f"worker-{name}.log")]  # fmt: skip
            return proc.start(argv, extra_env=env_vars, stderr_to=tmp / f"worker-{name}.err")

        first = worker("a")
        card = c.Card("INC-DRILL", "H1", 0, f"drill:{mode}", "main")
        handle = await env.client.start_workflow(
            SurgeonDrill.run, args=[card, DRILL_CONFIG], id=f"drill-{mode}", task_queue=CREW,
            execution_timeout=DRILL_TIMEOUT,
        )  # fmt: skip
        worktree = repo / "wt" / "INC-DRILL" / "H1"
        deadline = time.monotonic() + 90
        while True:  # wait for attempt 1 to reach the state the drill kills it in
            nonces = log.nonces("INC-DRILL", "H1")
            committed = mode == "adopt" and nonces and _branch_head(repo) != _main(repo)
            dirty = mode == "cleanup" and (worktree / "scratch.txt").exists()
            if committed or dirty:
                break
            if time.monotonic() > deadline:
                raise TimeoutError((tmp / "worker-a.err").read_text("utf-8", "replace")[-2000:])
            await asyncio.sleep(0.2)
        attempt1_head = _branch_head(repo)
        proc.kill_tree(first.pid)  # S-18: kill -9 of the Surgeon pool
        assert first.wait(timeout_s=10) is not None
        second = worker("b")
        try:
            outcome: c.SurgeonOutcome = await asyncio.wait_for(handle.result(), timeout=180)
            history = await handle.fetch_history()
        finally:
            second.kill_tree()
    return outcome, history, worktree, log, attempt1_head


def _branch_head(repo: Path) -> str:
    found = proc.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
                      "refs/heads/kintsugi/INC-DRILL/H1"], timeout_s=30)  # fmt: skip
    return found.stdout.strip()


def _main(repo: Path) -> str:
    return git(repo, "rev-parse", "main")


def retried_after_heartbeat_timeout(history: Any) -> bool:
    for event in history.events:
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_STARTED:
            attrs = event.activity_task_started_event_attributes
            failure = attrs.last_failure.timeout_failure_info
            if attrs.attempt == 2 and failure.timeout_type == TimeoutType.TIMEOUT_TYPE_HEARTBEAT:
                return True
    return False


def test_kill9_then_attempt_two_adopts_the_commit_by_its_nonce(tmp_path: Path) -> None:
    outcome, history, _, log, head = asyncio.run(drill("adopt", tmp_path))
    assert retried_after_heartbeat_timeout(history)  # the server's own record (T8)
    assert outcome.adopted
    assert outcome.attempt == 2
    assert outcome.commit_sha == head  # attempt 1's commit, not a second one
    assert [state for _, state, _, _ in log.history("INC-DRILL", "H1")] == ["adopted"]
    (tmp_path / "history-adopt.json").write_text(history.to_json(), encoding="utf-8")


def test_s18_through_the_watchdog_kill_restart_adopt(tmp_path: Path) -> None:
    """§6.6 end to end: chaos API kill → Watchdog restart → heartbeat timeout → adoption."""
    from fastapi.testclient import TestClient

    from kintsugi.control import watchdog as w
    from kintsugi.control.activities import SURGEON

    async def scenario() -> tuple[c.SurgeonOutcome, Any, w.Health, list[str]]:
        repo = make_repo(tmp_path)
        attempts = tmp_path / "attempts.sqlite"
        log = AttemptLog(attempts)
        async with await WorkflowEnvironment.start_local() as env:
            target = env.client.service_client.config.target_host
            pager = w.Pager(tmp_path / "pages.jsonl")
            sup = w.Supervisor(pager)
            argv = [sys.executable, "-m", "kintsugi.control.worker", "--target", target,
                    "--pool", "crew", "--pool", "surgeon", "--crew", "kintsugi.control.drill:crew",
                    "--workflows", "kintsugi.control.drill:WORKFLOWS",
                    "--ledger", str(tmp_path / "steps.sqlite"),
                    "--heartbeat-interval", "0.5"]  # fmt: skip
            sup.add(w.Pool("surgeon", argv, {REPO_ENV: str(repo), LEDGER_ENV: str(attempts)},
                           log=tmp_path / "pool.log"))  # fmt: skip
            dog = w.Watchdog(sup, pager, chaos_log=tmp_path / "chaos.jsonl")
            chaos = TestClient(w.chaos_app(dog, "k" * 43))
            card = c.Card("INC-DRILL", "H1", 0, "drill:adopt", "main")
            handle = await env.client.start_workflow(
                SurgeonDrill.run, args=[card, DRILL_CONFIG], id="drill-s18", task_queue=CREW,
                execution_timeout=DRILL_TIMEOUT,
            )  # fmt: skip
            deadline = time.monotonic() + 90
            while not (log.nonces("INC-DRILL", "H1") and _branch_head(repo) != _main(repo)):
                assert time.monotonic() < deadline
                await asyncio.sleep(0.2)
            killed = chaos.post("/chaos/kill", json={"pool": "surgeon", "sabotage_id": "S-18"},
                                headers={"Authorization": "Bearer " + "k" * 43})  # fmt: skip
            assert killed.status_code == 200
            restarted = sup.tick()
            try:
                outcome = await asyncio.wait_for(handle.result(), timeout=180)
                history = await handle.fetch_history()
                health = await asyncio.to_thread(
                    w.temporal_pollers_check(target, [CREW, SURGEON], max_age_s=60)
                )
            finally:
                sup.stop()
        return outcome, history, health, restarted

    outcome, history, health, restarted = asyncio.run(scenario())
    assert restarted == ["surgeon"]
    assert retried_after_heartbeat_timeout(history)
    assert outcome.adopted
    assert health.ok, health.detail


def test_kill9_then_attempt_two_cleans_up_and_commits_under_a_new_nonce(tmp_path: Path) -> None:
    outcome, history, worktree, log, _ = asyncio.run(drill("cleanup", tmp_path))
    assert retried_after_heartbeat_timeout(history)
    assert not outcome.adopted
    assert outcome.attempt == 2
    assert outcome.commit_sha is not None
    assert not (worktree / "scratch.txt").exists()
    states = [state for _, state, _, _ in log.history("INC-DRILL", "H1")]
    assert states == ["abandoned", "committed"]
    first_nonce = log.nonces("INC-DRILL", "H1")[0]
    assert outcome.nonce != first_nonce
    assert len(outcome.abandoned) == 1
    exported = json.loads(history.to_json())
    assert exported["events"]
