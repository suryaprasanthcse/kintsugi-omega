"""Shared runtime for KINTSUGI-Ω's own hosts (Ledger, Scout, Triage).

A host is an ordinary AI agent: it never imports Omega. It reaches the outside world only
through two environment-configured doors, both run by Omega outside the sandbox:

- ``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_API_KEY``: the LLM metering proxy and a per-host
  ``kintsugi-proxy-`` token (never a real key);
- ``KINTSUGI_EXT_URL``: the chaos proxy, in front of every "external" service the host's tools
  call (``{KINTSUGI_EXT_URL}/{service}/{path}``).

Every call carries the current task id (``x-kintsugi-task``) so faults and cassettes are keyed
per task.
"""

from hostkit.context import current_task, task_context

__all__ = ["current_task", "task_context"]
