"""The ledger database: journal lines in USD cents, one row per posting."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class JournalLine:
    key: str  # "<document ref>#<n>": which draft line produced the posting
    ref: str  # the source document's reference
    account: str
    amount: Decimal  # USD, debit positive, credit negative
    date: str  # ISO date
    currency: str = "USD"

    @property
    def cents(self) -> int:
        return int((self.amount * 100).to_integral_value())

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "ref": self.ref,
            "account": self.account,
            "amount_cents": self.cents,
            "currency": self.currency,
            "date": self.date,
        }


class LedgerDB:
    """SQLite-backed journal. ``insert`` has no idempotency: posting twice books twice."""

    def __init__(self, path: str = ":memory:") -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS journal (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "key TEXT NOT NULL, ref TEXT NOT NULL, account TEXT NOT NULL, "
            "amount_cents INTEGER NOT NULL, currency TEXT NOT NULL, date TEXT NOT NULL)"
        )

    def insert(self, line: JournalLine) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO journal (key, ref, account, amount_cents, currency, date) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (line.key, line.ref, line.account, line.cents, line.currency, line.date),
            )

    def lines(self, ref: str | None = None) -> list[JournalLine]:
        sql = "SELECT key, ref, account, amount_cents, currency, date FROM journal"
        args: tuple[str, ...] = ()
        if ref is not None:
            sql, args = sql + " WHERE ref = ?", (ref,)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY id", args).fetchall()
        return [JournalLine(k, r, a, Decimal(c) / 100, d, cur) for k, r, a, c, cur, d in rows]
