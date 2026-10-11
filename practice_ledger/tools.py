"""The practice ledger's tools: an FX rate lookup and the books API (``post_line`` books)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from neatlogs import span

from hostkit import external
from practice_ledger.retry import RateLimited


class FxUnavailable(RuntimeError):
    reason_code = "FX_UNAVAILABLE"


class BooksError(RuntimeError):
    reason_code = "BOOKS_ERROR"


@span(kind="TOOL", name="fx_rate")
def fx_rate(currency: str, date: str) -> Decimal:
    """USD per unit of ``currency`` on ``date``."""
    response = external.call("fx", "GET", "/rate", params={"currency": currency, "date": date})
    if response.status_code == 429:
        raise RateLimited(f"fx_rate {currency}: HTTP 429")
    if response.status_code >= 400:
        raise FxUnavailable(f"fx_rate {currency}: HTTP {response.status_code}")
    return Decimal(str(response.json()["rate"]))


@span(kind="TOOL", name="post_line")
def post_line(line: dict[str, Any]) -> None:
    """Book one journal line. The books API has no idempotency: posting twice books twice."""
    response = external.call("books", "POST", "/lines", json=line)
    if response.status_code >= 400:
        raise BooksError(f"post_line {line.get('key')}: HTTP {response.status_code}")


@span(kind="TOOL", name="list_lines")
def list_lines(ref: str) -> list[dict[str, Any]]:
    response = external.call("books", "GET", "/lines", params={"ref": ref})
    if response.status_code >= 400:
        raise BooksError(f"list_lines {ref}: HTTP {response.status_code}")
    return list(response.json()["lines"])
