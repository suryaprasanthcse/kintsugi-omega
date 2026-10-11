"""The document store behind ``fetch_document``: ``GET /doc/{id}?page=n``."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from scout.corpus import world


def handle(method: str, path: str, query: Mapping[str, str], body: bytes) -> tuple[int, Any]:
    prefix = "/doc/"
    if method != "GET" or not path.startswith(prefix):
        return 404, {"error": "not found"}
    doc = world().docs.get(path.removeprefix(prefix))
    page = query.get("page", "1")
    if doc is None or not page.isdigit() or not 1 <= int(page) <= len(doc.pages):
        return 404, {"error": f"no page {page} of {path.removeprefix(prefix)!r}"}
    return 200, {"doc_id": doc.id, "title": doc.title, "page": int(page),
                 "pages": len(doc.pages), "text": doc.pages[int(page) - 1]}  # fmt: skip
