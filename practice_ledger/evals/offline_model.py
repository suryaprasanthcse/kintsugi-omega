"""The practice ledger calls no model; the fabric still needs a labelled offline responder."""

from __future__ import annotations

from typing import Any


def respond(request: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": "{}"}]}
