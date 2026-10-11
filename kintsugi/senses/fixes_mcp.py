"""The neatlogs Fixes integration over MCP: adapter skeleton (spec §4.2; spike A4 is Stage 6).

The Coordinator uses five Fixes tools: ``triage_list`` (Fix suggestions for an incident's
traces), ``triage_get``, ``triage_get_investigation`` (a prior root-cause hypothesis),
``triage_accept`` (claim the suggestion) and ``triage_update_status`` (resolved or deployed,
with the PR link). Whether the Fixes MCP is available on this plan is spike A4, so Stage 4
defines only the interface and an implementation that reports the tools unavailable; the loop
runs without the prior until Stage 6 binds a real client.
"""

from __future__ import annotations

from typing import Any, Protocol

TOOLS = (
    "triage_list",
    "triage_get",
    "triage_get_investigation",
    "triage_accept",
    "triage_update_status",
)


class FixesUnavailable(RuntimeError):
    """The Fixes MCP is not connected; callers proceed without the prior hypothesis."""


class Fixes(Protocol):
    def triage_list(self, *, trace_ids: list[str]) -> list[dict[str, Any]]: ...

    def triage_get(self, suggestion_id: str) -> dict[str, Any]: ...

    def triage_get_investigation(self, suggestion_id: str) -> dict[str, Any]: ...

    def triage_accept(self, suggestion_id: str) -> dict[str, Any]: ...

    def triage_update_status(
        self, suggestion_id: str, status: str, *, pr_url: str | None = None
    ) -> dict[str, Any]: ...


class UnconfiguredFixes:
    """Every call raises ``FixesUnavailable`` until spike A4 binds the MCP (Stage 6)."""

    reason = "the neatlogs Fixes MCP is not configured (spike A4, Stage 6)"

    def _unavailable(self, *_: Any, **__: Any) -> Any:
        raise FixesUnavailable(self.reason)

    triage_list = triage_get = triage_get_investigation = _unavailable
    triage_accept = triage_update_status = _unavailable
