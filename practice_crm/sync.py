"""Syncing a batch of contacts to the CRM, each exactly once, with its region.

``sync_contacts`` looks each contact's region up through ``enrich`` (a geo API that may
rate-limit) and creates it through ``create_contact``. A 429 re-runs the attempt; the
``SyncSession`` records which contacts were created and where to resume.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from practice_crm.retry import RateLimited, retry_with_backoff
from practice_crm.tools import create_contact, enrich


@dataclass(frozen=True)
class Contact:
    key: str
    name: str
    domain: str


@dataclass(frozen=True)
class Batch:
    ref: str
    contacts: tuple[Contact, ...]


def build_batch(payload: dict[str, Any]) -> Batch:
    ref = payload["ref"]
    contacts = tuple(
        Contact(f"{ref}#{n}", item["name"], item["domain"])
        for n, item in enumerate(payload["contacts"], 1)
    )
    return Batch(ref, contacts)


class SyncSession:
    """Which contacts of one batch are created, and where to resume."""

    def __init__(self, batch: Batch) -> None:
        self.batch = batch
        self.created: set[str] = set()
        self.cursor = 0

    @property
    def done(self) -> bool:
        return self.cursor >= len(self.batch.contacts)

    def pending(self) -> tuple[Contact, ...]:
        return self.batch.contacts[self.cursor :]

    def push(self, contact: Contact, region: str) -> None:
        if contact.key not in self.created:
            create_contact({"key": contact.key, "ref": self.batch.ref, "name": contact.name,
                            "region": region})  # fmt: skip
            self.created.add(contact.key)
        self.cursor += 1


def sync_contacts(batch: Batch, session: SyncSession | None = None) -> SyncSession:
    """Create every pending contact of ``batch``; a rate limit retries the attempt."""

    def attempt() -> SyncSession:
        current = session or SyncSession(batch)
        for contact in current.pending():
            current.push(contact, enrich(contact.domain))
        return current

    return retry_with_backoff(attempt)


def sync_worker(
    batches: Iterable[Batch], sessions: dict[str, SyncSession] | None = None
) -> dict[str, SyncSession]:
    """Sync a queue of batches; a batch still rate-limited after its retries keeps its session
    and is resumed on the next pass."""
    sessions = {} if sessions is None else sessions
    for batch in batches:
        session = sessions.setdefault(batch.ref, SyncSession(batch))
        if session.done:
            continue
        try:
            sync_contacts(batch, session)
        except RateLimited:
            continue
    return sessions
