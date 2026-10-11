"""A deterministic FX API: ``GET /rate?currency=EUR&date=2026-09-14`` → ``{"rate": "1.0912"}``."""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

USD_PER_UNIT = {
    "USD": Decimal("1"),
    "EUR": Decimal("1.0850"),
    "GBP": Decimal("1.2700"),
    "JPY": Decimal("0.006720"),
    "INR": Decimal("0.011950"),
    "CAD": Decimal("0.7350"),
}
SERVICE_TIME_S = 0.008  # a paid API is not free in latency either


def rate(currency: str, date: str) -> Decimal:
    if currency == "USD":
        return Decimal(1)
    day = dt.date.fromisoformat(date).toordinal()
    wobble = Decimal(1) + Decimal((day % 17) - 8) / Decimal(1000)
    return (USD_PER_UNIT[currency] * wobble).quantize(Decimal("0.000001"))


def handle(method: str, path: str, query: Mapping[str, str], body: bytes) -> tuple[int, Any]:
    time.sleep(SERVICE_TIME_S)
    if method != "GET" or path != "/rate":
        return 404, {"error": "not found"}
    currency, date = query.get("currency", ""), query.get("date", "")
    if currency not in USD_PER_UNIT:
        return 400, {"error": f"unsupported currency {currency!r}"}
    try:
        value = rate(currency, date)
    except ValueError:
        return 400, {"error": f"bad date {date!r}"}
    return 200, {"currency": currency, "date": date, "rate": str(value)}
