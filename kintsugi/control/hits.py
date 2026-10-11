"""The senses → control-plane binding: the queue consumer of §5.6 ("queue consumer ──▶ Temporal
signal FleetSupervisor.hit(...)").

Stage 4's ingress and poller claim each hit once in the senses store and queue it; this
consumer takes queued items and hands them on:

- ``hit`` items become ``FleetSupervisor.hit`` signals (Temporal mode) or calls to any other
  ``deliver`` function (reduced mode, tests);
- ``alert`` items carry the Sentinel action the ingress mapped (§4.1.5); ``wake_watchdog``
  (``alert.ingestion_silent`` on ``kintsugi:*``) wakes the Watchdog, every alert goes to
  ``on_alert``.

The hand-off is at-least-once: items whose delivery fails are released back to the queue and
retried; the supervisor drops hit keys it has already seen, so a repeat is harmless.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from kintsugi.control import contracts as c

Deliver = Callable[[c.Hit], Awaitable[None]]


def to_hit(payload: dict[str, Any]) -> c.Hit:
    return c.Hit(
        hit_key=payload["hit_key"],
        detection_id=payload["detection_id"],
        trace_id=payload["trace_id"],
        span_id=payload["span_id"],
        source=payload.get("source", "poller"),
        receptor=payload.get("receptor"),
        host=payload.get("host"),
        detected_at=payload.get("detected_at"),
        hit_id=payload.get("hit_id"),
    )


def supervisor_deliver(client: Any, supervisor_id: str) -> Deliver:
    """Deliver hits as ``hit`` signals to the FleetSupervisor workflow."""

    async def deliver(hit: c.Hit) -> None:
        await client.get_workflow_handle(supervisor_id).signal("hit", hit)

    return deliver


@dataclass
class Forwarder:
    store: Any  # kintsugi.senses.store.SenseStore
    deliver: Deliver
    on_alert: Callable[[dict[str, Any]], None] | None = None
    wake: Callable[[str], Any] | None = None
    delivered: int = 0
    released: int = 0
    alerts: list[dict[str, Any]] = field(default_factory=list)

    async def drain(self, limit: int = 100) -> int:
        """One pass: take up to ``limit`` items and hand them on; returns how many."""
        items = self.store.take(limit)
        failed: list[int] = []
        for item in items:
            try:
                if item.kind == "hit":
                    await self.deliver(to_hit(item.payload))
                    self.delivered += 1
                else:
                    self._alert(item.payload)
            except Exception:
                failed.append(item.seq)
        if failed:
            self.store.release(failed)
            self.released += len(failed)
        return len(items) - len(failed)

    def _alert(self, payload: dict[str, Any]) -> None:
        self.alerts.append(payload)
        if payload.get("action") == "wake_watchdog" and self.wake is not None:
            self.wake(f"ingestion silent: {payload.get('event_id')}")
        if self.on_alert is not None:
            self.on_alert(payload)

    async def run(self, stop: threading.Event, *, interval_s: float = 1.0) -> None:
        while not stop.is_set():
            moved = await self.drain()
            if not moved:
                await asyncio.sleep(interval_s)
