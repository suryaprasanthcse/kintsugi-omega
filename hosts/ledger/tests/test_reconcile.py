from __future__ import annotations

from decimal import Decimal

import pytest

from ledger import reconcile as rc
from ledger.db import LedgerDB
from ledger.tools import ParsedDocument, ParsedLine
from ledger.util.retry import RateLimited, retry_with_backoff
from ledger.vendors import find_vendor


def _invoice(ref: str = "INV-1", doc_type: str = "invoice") -> ParsedDocument:
    lines = (ParsedLine("Cloud compute", Decimal("100.00"), "item"),
             ParsedLine("Storage", Decimal("50.10"), "item"),
             ParsedLine("Freight", Decimal("9.90"), "shipping"))  # fmt: skip
    return ParsedDocument(doc_type, "Brightline Cloud GmbH", ref, "2026-09-14", "EUR", lines,
                          Decimal("30.40"), Decimal("190.40"), None, None)  # fmt: skip


class Fx:
    """A stand-in FX tool: a fixed rate, optionally rate-limited on chosen calls."""

    def __init__(self, fail_on: set[int] | None = None, rate: str = "1.1") -> None:
        self.calls = 0
        self.fail_on = fail_on or set()
        self.rate = Decimal(rate)

    def __call__(self, currency: str, date: str) -> Decimal:
        self.calls += 1
        if self.calls in self.fail_on:
            raise RateLimited("429")
        return self.rate


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ledger.util.retry.time.sleep", lambda s: None)


def test_an_invoice_books_each_line_once_and_balances(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rc, "fx_rate", Fx())
    db = LedgerDB()
    batch = rc.build_batch(_invoice(), find_vendor("Brightline Cloud GmbH"))
    rc.process_invoice(db, batch)
    booked = [(x.account, x.amount) for x in db.lines("INV-1")]
    assert booked == [("6200", Decimal("110.00")), ("6200", Decimal("55.11")),
                      ("6300", Decimal("10.89")), ("1400", Decimal("33.44")),
                      ("2000", Decimal("-209.44"))]  # fmt: skip
    assert sum(amount for _, amount in booked) == 0


def test_credit_notes_and_payments_reverse_the_sides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rc, "fx_rate", Fx(rate="1"))
    db = LedgerDB()
    vendor = find_vendor("Brightline Cloud GmbH")
    rc.process_invoice(db, rc.build_batch(_invoice("CN-1", "credit_note"), vendor))
    assert db.lines("CN-1")[-1].amount == Decimal("190.40")  # payable debited
    payment = ParsedDocument("payment", vendor.name, "PAY-1", "2026-09-20", "EUR", (),
                             Decimal(0), Decimal("190.40"), "INV-1", Decimal("80.00"))  # fmt: skip
    rc.process_invoice(db, rc.build_batch(payment, vendor))
    assert [(x.account, x.amount) for x in db.lines("PAY-1")] == [
        ("2000", Decimal("80.00")), ("1000", Decimal("-80.00"))]  # fmt: skip


def test_a_payment_without_an_amount_is_refused() -> None:
    vendor = find_vendor("Harbor Telecom LLC")
    payment = ParsedDocument("payment", vendor.name, "PAY-2", "2026-09-20", "USD", (),
                             Decimal(0), Decimal(10), "INV-9", None)  # fmt: skip
    with pytest.raises(rc.IncompleteDocument):
        rc.build_batch(payment, vendor)


def test_resume_after_partial_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A batch still rate-limited after its retries resumes where it stopped, booking no line
    twice and the balancing line once."""
    batch = rc.build_batch(_invoice(), find_vendor("Brightline Cloud GmbH"))
    db = LedgerDB()
    # Lines 1 and 2 post, then every remaining attempt hits a 429 on line 3.
    monkeypatch.setattr(rc, "fx_rate", Fx(fail_on={3, 4, 5, 6, 7}))
    sessions = rc.batch_worker(db, [batch])
    session = sessions[batch.ref]
    assert not session.done
    assert session.cursor == 2
    assert [x.key for x in db.lines()] == ["INV-1#1", "INV-1#2"]
    monkeypatch.setattr(rc, "fx_rate", Fx())
    rc.batch_worker(db, [batch], sessions)
    assert session.done
    keys = [x.key for x in db.lines()]
    assert keys == ["INV-1#1", "INV-1#2", "INV-1#3", "INV-1#tax", "INV-1#bal"]
    assert sum(x.amount for x in db.lines()) == 0


def test_retry_with_backoff_gives_up_after_its_attempts() -> None:
    calls = []

    def always_limited() -> None:
        calls.append(1)
        raise RateLimited("429")

    with pytest.raises(RateLimited):
        retry_with_backoff(always_limited, attempts=3)
    assert len(calls) == 3
