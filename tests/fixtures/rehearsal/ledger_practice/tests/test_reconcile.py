from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from practice_ledger import reconcile as rc
from practice_ledger.retry import RateLimited, retry_with_backoff

DOCUMENT = {"ref": "INV-1", "currency": "EUR", "date": "2026-09-14",
            "items": [{"account": "6100-cloud", "amount": "100.00"},
                      {"account": "6200-software", "amount": "50.10"},
                      {"account": "6400-freight", "amount": "9.90"}]}  # fmt: skip


class Fx:
    """A stand-in FX tool: a fixed rate, optionally rate-limited on chosen calls."""

    def __init__(self, fail_on: set[int] | None = None) -> None:
        self.calls = 0
        self.fail_on = fail_on or set()

    def __call__(self, currency: str, date: str) -> Decimal:
        self.calls += 1
        if self.calls in self.fail_on:
            raise RateLimited("429")
        return Decimal("1.1")


@pytest.fixture
def books(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    booked: list[dict[str, Any]] = []
    monkeypatch.setattr(rc, "post_line", booked.append)
    monkeypatch.setattr("practice_ledger.retry.time.sleep", lambda s: None)
    return booked


def test_books_each_line_once_in_usd(books: list[dict[str, Any]], monkeypatch) -> None:
    monkeypatch.setattr(rc, "fx_rate", Fx())
    session = rc.reconcile(rc.build_batch(DOCUMENT))
    assert session.done
    assert [b["key"] for b in books] == ["INV-1#1", "INV-1#2", "INV-1#3"]
    assert [b["amount_cents"] for b in books] == [11000, 5511, 1089]


def test_a_rate_limit_before_any_booking_retries_cleanly(books, monkeypatch) -> None:
    monkeypatch.setattr(rc, "fx_rate", Fx(fail_on={1}))
    rc.reconcile(rc.build_batch(DOCUMENT))
    assert [b["key"] for b in books] == ["INV-1#1", "INV-1#2", "INV-1#3"]


def test_resume_after_partial_batch(books, monkeypatch) -> None:
    """A batch still rate-limited after its retries resumes where it stopped, booking no line
    twice."""
    batch = rc.build_batch(DOCUMENT)
    monkeypatch.setattr(rc, "fx_rate", Fx(fail_on={3, 4, 5, 6, 7}))
    sessions = rc.batch_worker([batch])
    session = sessions[batch.ref]
    assert not session.done
    assert session.cursor == 2
    assert [b["key"] for b in books] == ["INV-1#1", "INV-1#2"]
    monkeypatch.setattr(rc, "fx_rate", Fx())
    rc.batch_worker([batch], sessions)
    assert session.done
    assert [b["key"] for b in books] == ["INV-1#1", "INV-1#2", "INV-1#3"]


def test_retry_with_backoff_gives_up_after_its_attempts(monkeypatch) -> None:
    monkeypatch.setattr("practice_ledger.retry.time.sleep", lambda s: None)
    calls = []

    def always_limited() -> None:
        calls.append(1)
        raise RateLimited("429")

    with pytest.raises(RateLimited):
        retry_with_backoff(always_limited, attempts=3)
    assert len(calls) == 3
