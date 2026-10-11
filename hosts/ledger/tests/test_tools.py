from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest

from ledger import tools
from ledger.db import LedgerDB
from ledger.services import fx_api
from ledger.util.retry import RateLimited
from ledger.vendors import UnknownVendor, find_vendor


def _reply(status: int, body: Any) -> httpx.Response:
    return httpx.Response(status, json=body)


def test_fx_rate_maps_a_429_to_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def fake_call(service: str, method: str, path: str, **kw: Any) -> httpx.Response:
        seen.append({"service": service, "path": path, **kw})
        return _reply(429, {"type": "error"})

    monkeypatch.setattr(tools.external, "call", fake_call)
    with pytest.raises(RateLimited):
        tools.fx_rate("EUR", "2026-09-14")
    assert seen[0]["service"] == "fx"
    assert seen[0]["params"] == {"base": "EUR", "quote": "USD", "date": "2026-09-14"}


def test_fx_rate_reads_the_api_and_never_calls_out_for_usd(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_call(service: str, method: str, path: str, **kw: Any) -> httpx.Response:
        status, body = fx_api.handle(method, path, kw["params"], b"")
        return _reply(status, body)

    monkeypatch.setattr(tools.external, "call", fake_call)
    assert tools.fx_rate("EUR", "2026-09-14") == fx_api.rate("EUR", "2026-09-14")
    monkeypatch.setattr(tools.external, "call", None)
    assert tools.fx_rate("USD", "2026-09-14") == 1


def test_the_fx_api_is_deterministic_and_validates() -> None:
    assert fx_api.rate("JPY", "2026-08-03") == fx_api.rate("JPY", "2026-08-03")
    assert fx_api.rate("EUR", "2026-08-03") != fx_api.rate("EUR", "2026-08-04")
    assert fx_api.handle("GET", "/rate", {"base": "XXX", "date": "2026-08-03"}, b"")[0] == 400
    assert fx_api.handle("GET", "/rate", {"base": "EUR", "date": "soon"}, b"")[0] == 400
    assert fx_api.handle("POST", "/rate", {}, b"")[0] == 404


def test_vendor_lookup_tolerates_suffixes_and_case() -> None:
    assert tools.lookup_vendor("BRIGHTLINE CLOUD").id == "V-1002"
    assert tools.lookup_vendor("Sakura Components KK").id == "V-1004"
    with pytest.raises(UnknownVendor):
        tools.lookup_vendor("Brightline Cloud", exact=True)
    with pytest.raises(UnknownVendor):
        find_vendor("Globex Corporation")


def test_post_journal_entry_has_no_idempotency() -> None:
    db = LedgerDB()
    line = tools.JournalLine("INV-1#1", "INV-1", "6200", Decimal("10.00"), "2026-09-14")
    tools.post_journal_entry(db, line)
    tools.post_journal_entry(db, line)
    assert len(db.lines("INV-1")) == 2  # posting twice books twice: the S-17 hazard


def test_the_report_formats_amounts_and_checks_balance() -> None:
    db = LedgerDB()
    db.insert(tools.JournalLine("R#1", "R", "6200", Decimal("1234.50"), "2026-09-14"))
    db.insert(tools.JournalLine("R#bal", "R", "2000", Decimal("-1234.50"), "2026-09-14"))
    report = tools.reconcile_report(db, "R")
    assert report == {"ref": "R", "postings": 2, "debits": "1,234.50", "credits": "1,234.50",
                      "balanced": True}  # fmt: skip
    assert tools.fmt_amount(Decimal("1234567.891")) == "1,234,567.89"


def test_an_inconsistent_extraction_is_detected() -> None:
    doc = tools.ParsedDocument("invoice", "x", "INV-1", "2026-09-14", "EUR",
                               (tools.ParsedLine("a", Decimal(10), "item"),), Decimal(1),
                               Decimal(20), None, None)  # fmt: skip
    assert tools._inconsistency(doc) is not None
