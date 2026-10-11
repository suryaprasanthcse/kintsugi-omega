"""neatlogs instrumentation for a host process (spec §4.1.1, §4.1.3).

``init_host`` must run **before** ``anthropic`` (or any module that imports it) is imported.
Without ``NEATLOGS_API_KEY`` spans are created but not exported, so offline runs and unit tests
behave the same way. Inside a sandbox the key is a ``kintsugi-proxy-`` placeholder and
``NEATLOGS_ENDPOINT`` points at the egress gateway, which injects the real key.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from contextlib import AbstractContextManager
from typing import Any

import neatlogs
from neatlogs import span

_started = False


def init_host(host: str, *, tags: Sequence[str] = ()) -> bool:
    """Start the SDK for ``host:<id>``; returns whether spans are exported."""
    global _started
    key = os.environ.get("NEATLOGS_API_KEY")
    export = bool(key)
    neatlogs.init(
        api_key=key,
        workflow_name=f"host:{host}",
        instrumentations=["anthropic"],
        tags=[f"host:{host}", *tags],
        disable_export=not export,
    )
    _started = True
    return export


def shutdown() -> None:
    """Scripts flush and then shut down at exit (spec §4.1.1)."""
    global _started
    if _started:
        neatlogs.flush()
        neatlogs.shutdown()
        _started = False


def task_trace(task_id: str) -> AbstractContextManager[Any]:
    """The root span of one task, named ``task:<id>``.

    The agent's spans and its ``grader.verdict`` share this trace, so a receptor hit on the
    verdict (R-007) leads straight to the tool and model spans that produced it. Without it the
    verdict was exported as a one-span trace of its own (Stage 4 contract spike, A-28).
    """
    return neatlogs.trace(f"task:{task_id}", kind="WORKFLOW")


@span(kind="CHAIN", name="grader.verdict")
def grader_verdict(task_id: str, verdict: str) -> str:
    """The span receptor R-007 matches: output ``PASS`` or ``FAIL:<reason_code>`` (§4.1.4)."""
    if verdict != "PASS" and not verdict.startswith("FAIL:"):
        raise ValueError(f"a verdict is PASS or FAIL:<reason>, got {verdict!r}")
    return verdict
