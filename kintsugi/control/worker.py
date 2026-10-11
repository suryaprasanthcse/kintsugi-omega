"""Worker processes: one Temporal worker per pool, each a separate process (spec §5.2, §6.6).

    python -m kintsugi.control.worker --target localhost:7233 --pool crew --pool senses
    python -m kintsugi.control.worker --target localhost:7233 --pool surgeon

The pools are separate task queues, so the Watchdog's chaos API can ``kill -9`` the Surgeon
pool alone while the senses pool (the poller's path) keeps running (§6.6). The crew pool also
runs the workflows. ``--crew module:attr`` names the factory of the ``Crew`` port (Stage 6 binds
the real roles); ``--ledger`` is the SQLite step ledger every worker on the machine shares.
Output goes to ``--log`` so a supervised child never blocks on a full pipe.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from temporalio.client import Client
from temporalio.worker import Worker

from kintsugi.control.activities import CREW, SENSES, SURGEON, OmegaActivities, SqliteLedger

POOLS = {"crew": CREW, "surgeon": SURGEON, "senses": SENSES}


def resolve(dotted: str) -> Any:
    from kintsugi.fleet.hosts import resolve as resolve_attr

    return resolve_attr(dotted)


def build_workers(
    client: Client, acts: OmegaActivities, pools: Sequence[str], workflows: Sequence[type]
) -> list[Worker]:
    workers = []
    for pool in pools:
        queue = POOLS[pool]
        workers.append(Worker(client, task_queue=queue, activities=acts.activities_for(queue),
                              workflows=list(workflows) if pool == "crew" else []))  # fmt: skip
    return workers


async def serve(args: argparse.Namespace) -> None:
    client = await Client.connect(args.target, namespace=args.namespace)
    crew = resolve(args.crew)()
    acts = OmegaActivities(crew, SqliteLedger(args.ledger),
                           heartbeat_interval_s=args.heartbeat_interval)  # fmt: skip
    workers = build_workers(client, acts, args.pool, resolve(args.workflows))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Windows has no loop signal handlers: Ctrl+C raises KeyboardInterrupt instead.
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    logging.info("worker up: pools=%s target=%s", args.pool, args.target)
    runs = [asyncio.ensure_future(w.run()) for w in workers]
    try:
        await stop.wait()
    finally:
        await asyncio.gather(*(w.shutdown() for w in workers), return_exceptions=True)
        await asyncio.gather(*runs, return_exceptions=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Temporal workers for KINTSUGI-Ω pools.")
    parser.add_argument("--target", default="localhost:7233")
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--pool", action="append", choices=sorted(POOLS), required=True)
    parser.add_argument("--crew", required=True, help="module:factory returning a Crew")
    parser.add_argument("--workflows", default="kintsugi.control.workflows:WORKFLOWS")
    parser.add_argument("--ledger", type=Path, default=Path(".state/control/steps.sqlite"))
    parser.add_argument("--heartbeat-interval", type=float, default=None)
    parser.add_argument("--log", type=Path, default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(filename=args.log, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")  # fmt: skip
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
