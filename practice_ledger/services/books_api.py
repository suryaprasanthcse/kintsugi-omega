"""A books API, one ledger per task: ``POST /lines`` books (no idempotency),
``GET /lines?ref=…`` lists a document's lines, ``GET /lines/<key>`` fetches one line."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from typing import Any

SERVICE_TIME_S = 0.012
_books: dict[str, list[dict[str, Any]]] = {}
_lock = threading.Lock()


def reset() -> None:
    with _lock:
        _books.clear()


def handle(
    method: str, path: str, query: Mapping[str, str], body: bytes, namespace: str = ""
) -> tuple[int, Any]:
    time.sleep(SERVICE_TIME_S)
    with _lock:
        lines = _books.setdefault(namespace, [])
        if method == "POST" and path == "/lines":
            line = json.loads(body or b"{}")
            if not isinstance(line, dict) or "key" not in line:
                return 400, {"error": "a line needs a key"}
            lines.append(dict(line))
            return 201, {"booked": line["key"]}
        if method == "GET" and path == "/lines":
            ref = query.get("ref")
            return 200, {"lines": [dict(x) for x in lines if ref is None or x.get("ref") == ref]}
        if method == "GET" and path.startswith("/lines/"):
            key = path.removeprefix("/lines/")
            found = [dict(x) for x in lines if x.get("key") == key]
            return (200, {"line": found[0]}) if found else (404, {"error": "no such line"})
    return 404, {"error": "not found"}
