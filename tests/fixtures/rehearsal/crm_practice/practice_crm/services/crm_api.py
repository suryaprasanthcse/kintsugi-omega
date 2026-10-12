"""A CRM API, one CRM per task: ``POST /contacts`` creates (no idempotency) and
``GET /contacts?ref=…`` lists a batch's contacts."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from typing import Any

SERVICE_TIME_S = 0.010
_crm: dict[str, list[dict[str, Any]]] = {}
_lock = threading.Lock()


def reset() -> None:
    with _lock:
        _crm.clear()


def handle(
    method: str, path: str, query: Mapping[str, str], body: bytes, namespace: str = ""
) -> tuple[int, Any]:
    time.sleep(SERVICE_TIME_S)
    with _lock:
        contacts = _crm.setdefault(namespace, [])
        if method == "POST" and path == "/contacts":
            contact = json.loads(body or b"{}")
            if not isinstance(contact, dict) or "key" not in contact:
                return 400, {"error": "a contact needs a key"}
            contacts.append(dict(contact))
            return 201, {"created": contact["key"]}
        if method == "GET" and path == "/contacts":
            ref = query.get("ref")
            listed = [dict(c) for c in contacts if ref is None or c.get("ref") == ref]
            return 200, {"contacts": listed}
    return 404, {"error": "not found"}
