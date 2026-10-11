"""A mock GitHub issues API: labels and comments, kept per task so evals never collide.

``POST /repos/{o}/{r}/issues/{n}/labels`` ``{"labels": [...]}``, ``POST .../comments``
``{"body": ...}`` and ``GET .../issues/{n}``. The chaos proxy forwards the task header, which
this mock uses as the namespace (a real deployment would be one repository per host).
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Mapping
from typing import Any

_ROUTE = re.compile(r"^/repos/[^/]+/[^/]+/issues/(\d+)(/labels|/comments)?$")
_lock = threading.Lock()
_state: dict[tuple[str, int], dict[str, Any]] = {}


def reset() -> None:
    with _lock:
        _state.clear()


def _issue(namespace: str, number: int) -> dict[str, Any]:
    return _state.setdefault((namespace, number), {"labels": [], "comments": []})


def handle(
    method: str, path: str, query: Mapping[str, str], body: bytes, namespace: str = ""
) -> tuple[int, Any]:
    match = _ROUTE.match(path)
    if match is None:
        return 404, {"message": "Not Found"}
    number, sub = int(match[1]), match[2]
    payload = json.loads(body) if body else {}
    with _lock:
        issue = _issue(namespace, number)
        if method == "GET" and sub is None:
            return 200, {"number": number, **issue}
        if method == "POST" and sub == "/labels":
            for label in payload.get("labels", []):
                if label not in issue["labels"]:
                    issue["labels"].append(label)
            return 200, [{"name": label} for label in issue["labels"]]
        if method == "POST" and sub == "/comments":
            issue["comments"].append(str(payload.get("body", "")))
            return 201, {"body": payload.get("body", "")}
    return 405, {"message": "Method Not Allowed"}
