"""The Watchdog: deterministic supervision, health, the chaos API and a dead-man switch (§5.5).

Not an agent: plain code with fixed rules.

- **Supervision.** Each worker pool is a child process (``proc.start``). A pool that exits is
  restarted, at most ``max_restarts`` times per ``window_s`` (5 per 10 minutes); past that the
  Watchdog pages and leaves the pool down.
- **Health checks.** Named checks return ``Health(ok, detail)``: Temporal poller freshness per
  task queue, the senses queue depth and the age of the last item the control plane took,
  ``entire graph doctor --json``, ``neatlogs doctor --local --json``, any HTTP health URL.
- **Chaos API (the Saboteur only).** A local HTTP endpoint behind a 256-bit bearer token:
  ``POST /chaos/kill {pool, sabotage_id}`` kills a pool's process tree (the supervisor then
  restarts it: the L2 drill, §6.6); ``POST /chaos/ingress {available, duration_s,
  sabotage_id}`` switches the webhook ingress off and back on. Every call needs a sabotage id
  and is appended to ``chaos.jsonl``; the Watchdog itself is never a target.
- **Dead-man switch.** The Watchdog touches a heartbeat file; ``deadman`` (run from outside,
  e.g. cron or a minimal Lambda) pages when that heartbeat goes stale. A neatlogs
  ``alert.ingestion_silent`` on ``kintsugi:*`` reaches ``wake()`` through the senses queue.
  Paging is allowed; human commits are not.
"""

from __future__ import annotations

import argparse
import contextlib
import hmac
import json
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from kintsugi.sandbox import proc

RESTARTS, WINDOW_S = 5, 600.0
WATCHDOG = "watchdog"


@dataclass(frozen=True)
class Health:
    ok: bool
    detail: str = ""


class Pager:
    """Where pages go: an append-only file (and, when configured, a webhook to a human)."""

    def __init__(self, path: Path, notify: Callable[[dict[str, Any]], None] | None = None):
        self.path = path
        self.notify = notify
        self.pages: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def page(self, reason: str, **detail: Any) -> None:
        record = {"at": time.time(), "reason": reason, **detail}
        with self._lock:
            self.pages.append(record)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        if self.notify is not None:
            with contextlib.suppress(Exception):
                self.notify(record)


@dataclass
class Pool:
    name: str
    argv: Sequence[str]
    extra_env: Mapping[str, str] = field(default_factory=dict)
    log: Path | None = None
    process: proc.Proc | None = None
    restarts: deque[float] = field(default_factory=deque)
    starts: int = 0
    paged: bool = False


class Supervisor:
    def __init__(
        self,
        pager: Pager,
        *,
        max_restarts: int = RESTARTS,
        window_s: float = WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.pager = pager
        self.max_restarts = max_restarts
        self.window_s = window_s
        self.clock = clock
        self.pools: dict[str, Pool] = {}
        self._lock = threading.RLock()

    def add(self, pool: Pool) -> None:
        if pool.name == WATCHDOG:
            raise ValueError("the Watchdog is not a pool")
        with self._lock:
            self.pools[pool.name] = pool
            self._start(pool)

    def _start(self, pool: Pool) -> None:
        pool.process = proc.start(pool.argv, extra_env=pool.extra_env, stderr_to=pool.log)
        pool.starts += 1
        threading.Thread(target=_drain, args=(pool.process, pool.log), daemon=True).start()

    def pid(self, name: str) -> int | None:
        pool = self.pools.get(name)
        return pool.process.pid if pool and pool.process else None

    def tick(self) -> list[str]:
        """Restart pools that exited, within the restart budget; returns the pools restarted."""
        restarted: list[str] = []
        with self._lock:
            for pool in self.pools.values():
                if pool.paged or pool.process is None or pool.process.poll() is None:
                    continue
                now = self.clock()
                while pool.restarts and now - pool.restarts[0] > self.window_s:
                    pool.restarts.popleft()
                if len(pool.restarts) >= self.max_restarts:
                    pool.paged = True
                    restarts = len(pool.restarts)
                    self.pager.page("restart budget exhausted", pool=pool.name,
                                    restarts=restarts, window_s=self.window_s)  # fmt: skip
                    continue
                pool.restarts.append(now)
                self._start(pool)
                restarted.append(pool.name)
        return restarted

    def kill(self, name: str) -> int:
        """kill -9 of a pool's process tree (the chaos API's only process action)."""
        if name == WATCHDOG:
            raise PermissionError("the Watchdog is never a chaos target")
        with self._lock:
            pool = self.pools[name]
            assert pool.process is not None
            pid = pool.process.pid
            proc.kill_tree(pid)
            pool.process.wait(timeout_s=10)
            return pid

    def stop(self) -> None:
        with self._lock:
            for pool in self.pools.values():
                if pool.process is not None and pool.process.poll() is None:
                    pool.process.kill_tree()
                pool.paged = True


def _drain(process: proc.Proc, log: Path | None) -> None:
    """Keep a child's stdout pipe empty (a full pipe would block it), appending to its log."""
    with contextlib.suppress(Exception):
        for line in process.lines():
            if log is not None:
                with log.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line + "\n")


# --------------------------------------------------------------------------- health


def command_check(argv: Sequence[str], *, timeout_s: float = 60.0) -> Callable[[], Health]:
    """A check that passes when the command exits 0 (``entire graph doctor --json`` …)."""

    def check() -> Health:
        try:
            result = proc.run(list(argv), timeout_s=timeout_s)
        except (proc.ProcError, OSError) as error:
            return Health(False, f"{type(error).__name__}: {error}"[:300])
        return Health(result.returncode == 0, (result.stdout or result.stderr)[-300:])

    return check


def queue_check(store: Any, *, max_pending: int = 500, max_idle_s: float = 900.0,
                clock: Callable[[], float] = time.time) -> Callable[[], Health]:  # fmt: skip
    """Senses queue depth and the age of the last item the control plane took."""

    def check() -> Health:
        pending = store.pending()
        last = store.last_taken_at()
        idle = None if last is None else clock() - last
        if pending > max_pending:
            return Health(False, f"{pending} items waiting")
        if pending and idle is not None and idle > max_idle_s:
            return Health(False, f"{pending} waiting; nothing taken for {idle:.0f}s")
        return Health(True, f"{pending} waiting")

    return check


def temporal_pollers_check(target: str, queues: Sequence[str], *, max_age_s: float = 120.0,
                           namespace: str = "default") -> Callable[[], Health]:  # fmt: skip
    """Every task queue has a poller seen within ``max_age_s`` (worker poll lag)."""

    def check() -> Health:
        import asyncio

        from temporalio.api.enums.v1 import TaskQueueType
        from temporalio.api.taskqueue.v1 import TaskQueue
        from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
        from temporalio.client import Client

        async def describe() -> list[str]:
            client = await Client.connect(target, namespace=namespace)
            stale = []
            for queue in queues:
                response = await client.workflow_service.describe_task_queue(
                    DescribeTaskQueueRequest(namespace=namespace, task_queue=TaskQueue(name=queue),
                                             task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY)
                )  # fmt: skip
                ages = [time.time() - p.last_access_time.ToSeconds() for p in response.pollers]
                if not ages or min(ages) > max_age_s:
                    stale.append(queue)
            return stale

        try:
            stale = asyncio.run(describe())
        except Exception as error:  # the server itself is down
            return Health(False, f"temporal unreachable: {error}"[:300])
        return Health(not stale, f"stale queues: {stale}" if stale else "pollers fresh")

    return check


@dataclass
class Watchdog:
    supervisor: Supervisor
    pager: Pager
    checks: dict[str, Callable[[], Health]] = field(default_factory=dict)
    heartbeat_path: Path | None = None
    ingress_toggle: Callable[[bool], None] | None = None
    chaos_log: Path | None = None
    failing: dict[str, str] = field(default_factory=dict)

    def run_checks(self) -> dict[str, Health]:
        results = {name: check() for name, check in self.checks.items()}
        for name, health in results.items():
            if not health.ok and name not in self.failing:
                self.pager.page("health check failing", check=name, detail=health.detail)
            if health.ok:
                self.failing.pop(name, None)
            else:
                self.failing[name] = health.detail
        return results

    def beat(self) -> None:
        if self.heartbeat_path is not None:
            self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            self.heartbeat_path.write_text(f"{time.time():.3f}\n", encoding="utf-8")

    def wake(self, reason: str) -> dict[str, Health]:
        """The dead-man path (``alert.ingestion_silent`` on kintsugi:*): check now, page if bad."""
        results = self.run_checks()
        if all(h.ok for h in results.values()):
            self.pager.page("woken: ingestion silent but every check passes", cause=reason)
        return results

    def loop(
        self, stop: threading.Event, *, interval_s: float = 5.0, checks_every: int = 12
    ) -> None:
        n = 0
        while not stop.wait(interval_s):
            self.supervisor.tick()
            self.beat()
            n += 1
            if n % checks_every == 0:
                self.run_checks()

    # ----------------------------------------------------------------------- chaos API

    def record(self, op: str, sabotage_id: str, **args: Any) -> None:
        if self.chaos_log is None:
            return
        self.chaos_log.parent.mkdir(parents=True, exist_ok=True)
        entry = {"at": time.time(), "op": op, "sabotage_id": sabotage_id, **args}
        with self.chaos_log.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")

    def outage(self, duration_s: float) -> None:
        assert self.ingress_toggle is not None
        self.ingress_toggle(False)
        timer = threading.Timer(duration_s, self.ingress_toggle, args=(True,))
        timer.daemon = True
        timer.start()


def chaos_app(watchdog: Watchdog, token: str) -> FastAPI:
    if len(token) < 43:
        raise ValueError("the chaos token must carry at least 256 bits")
    app = FastAPI(title="kintsugi-chaos", docs_url=None, redoc_url=None, openapi_url=None)

    def authorized(request: Request) -> bool:
        given = request.headers.get("authorization", "")
        return hmac.compare_digest(given.encode(), f"Bearer {token}".encode())

    @app.post("/chaos/kill")
    async def kill(request: Request) -> JSONResponse:
        if not authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        pool, sabotage = str(body.get("pool", "")), str(body.get("sabotage_id", ""))
        if not sabotage:
            return JSONResponse({"error": "every chaos call names its sealed sabotage"}, 400)
        if pool == WATCHDOG:
            return JSONResponse({"error": "the Watchdog is never a target"}, status_code=403)
        if pool not in watchdog.supervisor.pools:
            return JSONResponse({"error": f"no pool {pool!r}"}, status_code=404)
        pid = watchdog.supervisor.kill(pool)
        watchdog.record("kill", sabotage, pool=pool, pid=pid)
        return JSONResponse({"killed": pool, "pid": pid})

    @app.post("/chaos/ingress")
    async def ingress(request: Request) -> JSONResponse:
        if not authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        sabotage = str(body.get("sabotage_id", ""))
        if not sabotage:
            return JSONResponse({"error": "every chaos call names its sealed sabotage"}, 400)
        if watchdog.ingress_toggle is None:
            return JSONResponse({"error": "no ingress is configured"}, status_code=404)
        duration = float(body.get("duration_s", 0))
        if body.get("available", False):
            watchdog.ingress_toggle(True)
        elif duration > 0:
            watchdog.outage(duration)
        else:
            watchdog.ingress_toggle(False)
        watchdog.record("ingress", sabotage, available=bool(body.get("available")),
                        duration_s=duration)  # fmt: skip
        return JSONResponse({"ok": True})

    return app


def http_toggle(url: str, admin_token: str) -> Callable[[bool], None]:
    """Switch the ingress through its admin endpoint."""

    def toggle(available: bool) -> None:
        import httpx

        headers = {"Authorization": f"Bearer {admin_token}"}
        response = httpx.post(url, json={"available": available}, timeout=5, headers=headers)
        response.raise_for_status()

    return toggle


# --------------------------------------------------------------------------- dead man


def deadman(heartbeat: Path, max_age_s: float, pager: Pager, *, now: float | None = None) -> bool:
    """Run from outside the Watchdog: page when its heartbeat is missing or stale."""
    now = time.time() if now is None else now
    try:
        last = float(heartbeat.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pager.page("dead man: no watchdog heartbeat", path=str(heartbeat))
        return False
    if now - last > max_age_s:
        pager.page("dead man: watchdog heartbeat is stale", age_s=round(now - last, 1))
        return False
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Watchdog helpers.")
    sub = parser.add_subparsers(dest="command", required=True)
    dm = sub.add_parser("deadman", help="page if the Watchdog's heartbeat is stale")
    dm.add_argument("--heartbeat", type=Path, default=Path(".state/watchdog/heartbeat"))
    dm.add_argument("--max-age", type=float, default=120.0)
    dm.add_argument("--pages", type=Path, default=Path(".state/watchdog/pages.jsonl"))
    args = parser.parse_args(argv)
    alive = deadman(args.heartbeat, args.max_age, Pager(args.pages))
    print("watchdog alive" if alive else "watchdog DEAD: paged")
    return 0 if alive else 1


if __name__ == "__main__":
    sys.exit(main())
