"""Booking a document's lines in USD, exactly once each.

A document becomes a ``Batch`` of lines in its own currency. ``reconcile`` converts each line
through ``fx_rate`` (an external API that may rate-limit) and books it through ``post_line``. A
429 re-runs the whole attempt, so the ``PostingSession`` remembers which lines are already
booked and where to resume: a retry never books a line twice.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from practice_ledger.retry import RateLimited, retry_with_backoff
from practice_ledger.tools import fx_rate, post_line

CENT = Decimal("0.01")


@dataclass(frozen=True)
class Line:
    key: str
    account: str
    amount: Decimal  # document currency
    currency: str
    date: str


@dataclass(frozen=True)
class Batch:
    ref: str
    lines: tuple[Line, ...]


def build_batch(document: dict[str, Any]) -> Batch:
    ref, currency, date = document["ref"], document["currency"], document["date"]
    lines = tuple(
        Line(f"{ref}#{n}", item["account"], Decimal(item["amount"]), currency, date)
        for n, item in enumerate(document["items"], 1)
    )
    return Batch(ref, lines)


class PostingSession:
    """Which lines of one batch are booked, and where to resume."""

    def __init__(self, batch: Batch) -> None:
        self.batch = batch
        self.booked: set[str] = set()
        self.cursor = 0

    @property
    def done(self) -> bool:
        return self.cursor >= len(self.batch.lines)

    def pending(self) -> tuple[Line, ...]:
        return self.batch.lines[self.cursor :]

    def book(self, line: Line, rate: Decimal) -> None:
        if line.key not in self.booked:
            usd = (line.amount * rate).quantize(CENT, rounding=ROUND_HALF_UP)
            post_line({"key": line.key, "ref": self.batch.ref, "account": line.account,
                       "amount_cents": int(usd * 100), "date": line.date})  # fmt: skip
            self.booked.add(line.key)
        self.cursor += 1


def reconcile(batch: Batch, session: PostingSession | None = None) -> PostingSession:
    session = session or PostingSession(batch)  # outside the retried scope: survives retries

    def attempt() -> PostingSession:
        for line in session.pending():
            session.book(line, fx_rate(line.currency, line.date))
        return session

    return retry_with_backoff(attempt)


def batch_worker(
    batches: Iterable[Batch], sessions: dict[str, PostingSession] | None = None
) -> dict[str, PostingSession]:
    """Reconcile a queue of batches. A batch still rate-limited after its retries keeps its
    session, so the next pass resumes it where it stopped instead of starting over."""
    sessions = {} if sessions is None else sessions
    for batch in batches:
        session = sessions.setdefault(batch.ref, PostingSession(batch))
        if session.done:
            continue
        try:
            reconcile(batch, session)
        except RateLimited:
            continue  # resumed on the next pass
    return sessions
