"""The host's model client: the Anthropic SDK pointed at the LLM metering proxy.

``ANTHROPIC_BASE_URL`` is ``<proxy>/h/<host>`` and ``ANTHROPIC_API_KEY`` a per-host proxy token;
the SDK reads both from the environment. Import this module only after
``hostkit.telemetry.init_host`` so neatlogs instruments the SDK (spec §4.1.1 import order).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping, Sequence
from typing import Any

import anthropic

from hostkit.context import task_headers

_local = threading.local()


class ModelOutputError(ValueError):
    """The model's answer did not parse as the requested JSON."""


def client() -> anthropic.Anthropic:
    existing = getattr(_local, "client", None)
    if existing is None:
        existing = anthropic.Anthropic(max_retries=2)
        _local.client = existing
    return existing


def structured(
    *,
    model: str,
    system: str,
    messages: Sequence[Mapping[str, Any]],
    schema: Mapping[str, Any],
    max_tokens: int = 2048,
) -> dict[str, Any]:
    """One call constrained to ``schema`` (structured outputs); returns the parsed JSON."""
    response = client().messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=list(messages),  # type: ignore[arg-type]
        output_config={"format": {"type": "json_schema", "schema": dict(schema)}},
        extra_headers=task_headers(),
    )
    if response.stop_reason == "refusal":
        raise ModelOutputError("the model refused")
    text = "".join(block.text for block in response.content if block.type == "text")
    try:
        value = json.loads(text)
    except ValueError as error:
        raise ModelOutputError(f"not JSON: {text[:200]!r}") from error
    if not isinstance(value, dict):
        raise ModelOutputError("expected a JSON object")
    return value
