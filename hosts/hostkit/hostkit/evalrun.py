"""The eval runner: run a host over a task set and grade every task.

    python -m hostkit.evalrun --host ledger --split train --seed 1 --out results.jsonl
    python -m hostkit.evalrun --host ledger --split holdout --tasks-file -   # Omega pipes in the
                                                                             # decrypted holdout

For each task the host's agent runs under the task id; the host's grader then emits the
``grader.verdict`` span (``PASS`` or ``FAIL:<reason>``, spec §4.1.1) and one JSON line is
written: task, split, seed, verdict, latency. A host is a package ``<host>`` with
``<host>.agent.run_task(task) -> output``, ``<host>.graders.grader.grade(task, output)`` and
``<host>.evals.generate.tasks(split)``.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import importlib
import json
import re
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TextIO

from hostkit import telemetry
from hostkit.context import task_context

Agent = Callable[[dict[str, Any]], dict[str, Any]]
Grader = Callable[[dict[str, Any], dict[str, Any]], str]


def reason_code(error: BaseException) -> str:
    """A grader reason code for a task that raised: explicit, HTTP-derived or from the class."""
    explicit = getattr(error, "reason_code", None)
    if isinstance(explicit, str):
        return explicit
    status = getattr(error, "status_code", None)
    if status == 402:
        return "BUDGET_EXCEEDED"
    if isinstance(status, int):
        return f"MODEL_HTTP_{status}"
    return re.sub(r"(?<!^)(?=[A-Z])", "_", type(error).__name__).upper()


def run_one(agent: Agent, grader: Grader, task: dict[str, Any], split: str, seed: int) -> dict:
    task_id = str(task["id"])
    started = time.monotonic()
    with task_context(task_id):
        try:
            output = agent(task)
        except Exception as error:  # a failed task is graded, never crashes the run
            output = {"error": reason_code(error), "detail": str(error)[:300]}
        latency = time.monotonic() - started
        verdict = telemetry.grader_verdict(task_id, grader(task, output))
    return {"task": task_id, "split": split, "seed": seed, "verdict": verdict,
            "latency_s": round(latency, 4)}  # fmt: skip


def read_tasks(handle: TextIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in handle if line.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run and grade a host's eval tasks.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--split", required=True)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--phase", default="baseline")
    parser.add_argument("--tasks-file", help="JSONL of tasks, '-' for stdin (holdout)")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    run_id = uuid.uuid4().hex[:8]
    tags = [f"split:{args.split}", f"seed:{args.seed}", f"phase:{args.phase}", f"run:{run_id}"]
    telemetry.init_host(args.host, tags=tags)  # before the agent (and anthropic) is imported
    try:
        agent: Agent = importlib.import_module(f"{args.host}.agent").run_task
        grader: Grader = importlib.import_module(f"{args.host}.graders.grader").grade
        if args.tasks_file == "-":
            tasks = read_tasks(sys.stdin)
        elif args.tasks_file:
            with open(args.tasks_file, encoding="utf-8") as handle:
                tasks = read_tasks(handle)
        else:
            tasks = importlib.import_module(f"{args.host}.evals.generate").tasks(args.split)
        tasks = tasks[: args.limit] if args.limit else tasks
        with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(run_one, agent, grader, t, args.split, args.seed) for t in tasks]
            rows = [f.result() for f in futures]
    finally:
        telemetry.shutdown()
    lines = "".join(json.dumps(row) + "\n" for row in rows)
    if args.out:
        args.out.write_text(lines, encoding="utf-8")
    else:
        sys.stdout.write(lines)
    passed = sum(row["verdict"] == "PASS" for row in rows)
    print(f"{args.host} {args.split} seed={args.seed}: {passed}/{len(rows)} PASS", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
