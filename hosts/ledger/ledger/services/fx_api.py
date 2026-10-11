"""A deterministic FX rates API (the "external" service behind ``fx_rate``).

``GET /rate?base=EUR&quote=USD&date=2026-09-14`` → ``{"base", "quote", "date", "rate"}``.
Rates wobble ±1.1% around a base level with the day of the year, so documents dated
differently convert differently.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

USD_PER_UNIT: dict[str, Decimal] = {
    "USD": Decimal("1"),
    "EUR": Decimal("1.0850"),
    "GBP": Decimal("1.2700"),
    "JPY": Decimal("0.006720"),
    "INR": Decimal("0.011950"),
    "CAD": Decimal("0.7350"),
}


def rate(currency: str, date: str) -> Decimal:
    """USD per unit of ``currency`` on ``date``."""
    if currency == "USD":
        return Decimal(1)
    day = dt.date.fromisoformat(date).toordinal()
    wobble = Decimal(1) + Decimal((day % 23) - 11) / Decimal(1000)
    return (USD_PER_UNIT[currency] * wobble).quantize(Decimal("0.000001"))


def handle(method: str, path: str, query: Mapping[str, str], body: bytes) -> tuple[int, Any]:
    if method != "GET" or path != "/rate":
        return 404, {"error": "not found"}
    base, quote, date = query.get("base", ""), query.get("quote", "USD"), query.get("date", "")
    if base not in USD_PER_UNIT or quote != "USD":
        return 400, {"error": f"unsupported pair {base}/{quote}"}
    try:
        value = rate(base, date)
    except ValueError:
        return 400, {"error": f"bad date {date!r}"}
    return 200, {"base": base, "quote": quote, "date": date, "rate": str(value)}
