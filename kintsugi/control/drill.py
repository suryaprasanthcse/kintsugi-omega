"""The L2 crash drill (spec §6.6): a stand-in Surgeon session that can be killed mid-attempt.

``DrillSession`` runs ``python -m kintsugi.control.drill session …`` as the Surgeon's child
process, exactly where Stage 6 runs headless Claude Code. Behaviour per card brief:

- ``drill:adopt``   attempt 1 commits (with its nonce) and then keeps working until killed;
                    attempt 2 must adopt that commit without starting a session;
- ``drill:cleanup`` attempt 1 dirties the worktree and is killed before committing; attempt 2
                    must kill the orphan, reset and clean, and commit under a NEW nonce.

Any later attempt commits at once and exits. ``SurgeonDrill`` is a one-activity workflow that
runs the surgeon step through the real activity contract (heartbeats, retries, idempotency).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from kintsugi.control import contracts as c
    from kintsugi.control.workflows import WORKFLOWS as BASE_WORKFLOWS
    from kintsugi.control.workflows import act

REPO_ENV = "KINTSUGI_DRILL_REPO"
LEDGER_ENV = "KINTSUGI_DRILL_LEDGER"


@workflow.defn
class SurgeonDrill:
    @workflow.run
    async def run(self, card: c.Card, config: c.ControlConfig) -> c.SurgeonOutcome:
        return await act("surgeon", card, config=config, result_type=c.SurgeonOutcome)


WORKFLOWS = (*BASE_WORKFLOWS, SurgeonDrill)


class DrillSession:
    """Runs the stand-in session as a child process and waits for it."""

    def run(
        self,
        wt: Any,
        card: c.Card,
        nonce: str,
        attempt: int,
        on_start: Callable[[int], None],
    ) -> str | None:
        from kintsugi.sandbox import proc

        mode = card.brief.removeprefix("drill:")
        argv = [sys.executable, "-m", "kintsugi.control.drill", "session", "--worktree",
                str(wt.path), "--nonce", nonce, "--mode", mode, "--attempt", str(attempt),
                "--incident", card.incident_id, "--hypothesis", card.hypothesis]  # fmt: skip
        child = proc.start(argv, stderr_to=os.devnull)
        on_start(child.pid)
        sha: str | None = None
        for line in child.lines():
            if line.startswith("committed "):
                sha = line.split()[1]
        child.wait(timeout_s=600)
        return sha


def crew() -> Any:
    """The drill crew: only ``surgeon``, through the real ``SurgeonStep``."""
    from kintsugi.control.activities import ScriptedCrew, StepContext
    from kintsugi.control.surgeon_step import AttemptLog, SurgeonStep

    step = SurgeonStep(Path(os.environ[REPO_ENV]), AttemptLog(os.environ[LEDGER_ENV]),
                       DrillSession())  # fmt: skip

    class DrillCrew(ScriptedCrew):
        def handle(self, ctx: StepContext, *args: Any) -> Any:
            self.calls.append((ctx.name, ctx.attempt))
            if ctx.name == "surgeon":
                return step.run(args[0], ctx)
            raise NotImplementedError(ctx.name)

    return DrillCrew({})


def surgeon_drill_step(card: dict[str, Any], attempt: int) -> dict[str, Any]:
    """Reduced mode: one surgeon step for ``card`` at ``attempt``, as a JSON-able dict."""
    from dataclasses import asdict

    from kintsugi.control.activities import StepContext
    from kintsugi.control.surgeon_step import AttemptLog, SurgeonStep

    step = SurgeonStep(Path(os.environ[REPO_ENV]), AttemptLog(os.environ[LEDGER_ENV]),
                       DrillSession())  # fmt: skip
    outcome = step.run(c.Card(**card), StepContext("surgeon", "fsm", attempt, lambda *_: None))
    return asdict(outcome)


DRILL_CONFIG = c.ControlConfig(heartbeat_s=3.0)
DRILL_TIMEOUT = timedelta(minutes=5)


# --------------------------------------------------------------------------- the child session


def _commit(worktree: Path, attempt: int, nonce: str, incident: str, hypothesis: str) -> str:
    from kintsugi.sandbox import proc

    (worktree / "fix.txt").write_text(f"attempt {attempt}\n", encoding="utf-8")
    proc.run(["git", "-C", str(worktree), "add", "fix.txt"], timeout_s=60, check=True)
    message = (f"fix({incident}): drill attempt {attempt} for {hypothesis}\n\n"
               f"Kintsugi-Incident: {incident}\nKintsugi-Nonce: {nonce}\n")  # fmt: skip
    proc.run(["git", "-C", str(worktree), "-c", "user.name=Kintsugi Drill",
              "-c", "user.email=drill@kintsugi.invalid", "commit", "-q", "-F", "-"],
             input_text=message, timeout_s=60, check=True)  # fmt: skip
    head = proc.run(["git", "-C", str(worktree), "rev-parse", "HEAD"], timeout_s=30, check=True)
    return head.stdout.strip()


def session(args: argparse.Namespace) -> int:
    worktree = Path(args.worktree)
    print(f"started attempt {args.attempt} pid {os.getpid()}", flush=True)
    if args.attempt == 1 and args.mode == "adopt":
        sha = _commit(worktree, 1, args.nonce, args.incident, args.hypothesis)
        print(f"committed {sha}", flush=True)
        time.sleep(600)  # keep working until killed
        return 0
    if args.attempt == 1 and args.mode == "cleanup":
        (worktree / "scratch.txt").write_text("half-done work\n", encoding="utf-8")
        print("working", flush=True)
        time.sleep(600)  # killed before it ever commits
        return 0
    sha = _commit(worktree, args.attempt, args.nonce, args.incident, args.hypothesis)
    print(f"committed {sha}", flush=True)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The L2 drill's stand-in Surgeon session.")
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("session")
    for flag in ("--worktree", "--nonce", "--mode", "--incident", "--hypothesis"):
        s.add_argument(flag, required=True)
    s.add_argument("--attempt", type=int, required=True)
    args = parser.parse_args(argv)
    return session(args)


if __name__ == "__main__":
    sys.exit(main())
