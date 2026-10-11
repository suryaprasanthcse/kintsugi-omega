"""Reconciliation: post a document's draft lines in USD, exactly once each.

A document becomes a ``Batch`` of draft lines in its own currency plus one USD balancing line
(accounts payable for invoices and credit notes, cash for payments). ``run_reconcile`` converts
and posts the lines through ``fx_rate``, which an external API serves and may rate-limit; a 429
re-runs the whole attempt. The ``ReconcileSession`` remembers which lines are already posted
and where to resume, so a retry never books a line twice.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from ledger.db import JournalLine, LedgerDB
from ledger.tools import ParsedDocument, fx_rate, post_journal_entry
from ledger.util.retry import RateLimited, retry_with_backoff
from ledger.vendors import CASH, FREIGHT, INPUT_TAX, PAYABLE, Vendor

CENT = Decimal("0.01")


class IncompleteDocument(ValueError):
    reason_code = "INCOMPLETE_DOCUMENT"


@dataclass(frozen=True)
class DraftLine:
    key: str
    account: str
    amount: Decimal  # document currency; debit positive, credit negative
    currency: str
    date: str


@dataclass(frozen=True)
class Batch:
    ref: str
    date: str
    lines: tuple[DraftLine, ...]
    balance_account: str  # receives the USD balancing line


def build_batch(doc: ParsedDocument, vendor: Vendor) -> Batch:
    ref, currency, date = doc.reference, doc.currency, doc.date
    if doc.doc_type == "payment":
        if doc.amount_paid is None:
            raise IncompleteDocument(f"{ref}: a payment without an amount")
        paid = DraftLine(f"{ref}#1", PAYABLE, doc.amount_paid, currency, date)
        return Batch(ref, date, (paid,), CASH)
    sign = Decimal(-1) if doc.doc_type == "credit_note" else Decimal(1)
    drafts = [
        DraftLine(f"{ref}#{n}", FREIGHT if line.kind == "shipping" else vendor.expense_account,
                  sign * line.amount, currency, date)
        for n, line in enumerate(doc.lines, 1)
    ]  # fmt: skip
    if doc.tax:
        drafts.append(DraftLine(f"{ref}#tax", INPUT_TAX, sign * doc.tax, currency, date))
    return Batch(ref, date, tuple(drafts), PAYABLE)


class ReconcileSession:
    """Posted-line memory and the resume cursor for one batch."""

    def __init__(self, batch: Batch) -> None:
        self.batch = batch
        self.posted: set[str] = set()
        self.cursor = 0
        self.total = Decimal(0)

    @property
    def balance_key(self) -> str:
        return f"{self.batch.ref}#bal"

    @property
    def done(self) -> bool:
        return self.balance_key in self.posted

    def pending(self) -> tuple[DraftLine, ...]:
        return self.batch.lines[self.cursor :]

    def post(self, db: LedgerDB, line: DraftLine, rate: Decimal) -> None:
        if line.key not in self.posted:
            amount = (line.amount * rate).quantize(CENT, rounding=ROUND_HALF_UP)
            post_journal_entry(db, JournalLine(line.key, self.batch.ref, line.account, amount,
                                               line.date))  # fmt: skip
            self.posted.add(line.key)
            self.total += amount
        self.cursor += 1

    def close(self, db: LedgerDB) -> None:
        if not self.done:
            post_journal_entry(db, JournalLine(self.balance_key, self.batch.ref,
                                               self.batch.balance_account, -self.total,
                                               self.batch.date))  # fmt: skip
            self.posted.add(self.balance_key)


def run_reconcile(
    db: LedgerDB, batch: Batch, session: ReconcileSession | None = None
) -> ReconcileSession:
    session = session or ReconcileSession(batch)  # outside the retried scope: survives retries

    def attempt() -> ReconcileSession:
        for line in session.pending():
            rate = fx_rate(line.currency, line.date)
            session.post(db, line, rate)
        session.close(db)
        return session

    return retry_with_backoff(attempt)


def process_invoice(db: LedgerDB, batch: Batch) -> ReconcileSession:
    return run_reconcile(db, batch)


def batch_worker(
    db: LedgerDB, batches: Iterable[Batch], sessions: dict[str, ReconcileSession] | None = None
) -> dict[str, ReconcileSession]:
    """Reconcile a queue of batches. A batch still rate-limited after its retries keeps its
    session, so the next pass resumes it where it stopped instead of starting over."""
    sessions = {} if sessions is None else sessions
    for batch in batches:
        session = sessions.setdefault(batch.ref, ReconcileSession(batch))
        if session.done:
            continue
        try:
            run_reconcile(db, batch, session)
        except RateLimited:
            continue  # resumed on the next pass
    return sessions
