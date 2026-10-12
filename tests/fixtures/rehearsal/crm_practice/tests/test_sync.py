from __future__ import annotations

from typing import Any

import pytest

from practice_crm import sync
from practice_crm.retry import RateLimited, retry_with_backoff

PAYLOAD = {
    "ref": "B-1",
    "contacts": [
        {"name": "Ada Lovelace", "domain": "acme.example"},
        {"name": "Alan Turing", "domain": "hooli.test"},
        {"name": "Grace Hopper", "domain": "wonka.invalid"},
    ],
}


class Geo:
    """A stand-in geo tool: a fixed region, optionally rate-limited on chosen calls."""

    def __init__(self, fail_on: set[int] | None = None) -> None:
        self.calls = 0
        self.fail_on = fail_on or set()

    def __call__(self, domain: str) -> str:
        self.calls += 1
        if self.calls in self.fail_on:
            raise RateLimited("429")
        return "EMEA"


@pytest.fixture
def crm(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    created: list[dict[str, Any]] = []
    monkeypatch.setattr(sync, "create_contact", created.append)
    monkeypatch.setattr("practice_crm.retry.time.sleep", lambda s: None)
    return created


def test_creates_each_contact_once_with_its_region(crm, monkeypatch) -> None:
    monkeypatch.setattr(sync, "enrich", Geo())
    session = sync.sync_contacts(sync.build_batch(PAYLOAD))
    assert session.done
    assert [c["key"] for c in crm] == ["B-1#1", "B-1#2", "B-1#3"]
    assert {c["region"] for c in crm} == {"EMEA"}


def test_a_rate_limit_before_any_creation_retries_cleanly(crm, monkeypatch) -> None:
    monkeypatch.setattr(sync, "enrich", Geo(fail_on={1}))
    sync.sync_contacts(sync.build_batch(PAYLOAD))
    assert [c["key"] for c in crm] == ["B-1#1", "B-1#2", "B-1#3"]


def test_retry_with_backoff_gives_up_after_its_attempts(monkeypatch) -> None:
    monkeypatch.setattr("practice_crm.retry.time.sleep", lambda s: None)
    calls = []

    def always_limited() -> None:
        calls.append(1)
        raise RateLimited("429")

    with pytest.raises(RateLimited):
        retry_with_backoff(always_limited, attempts=3)
    assert len(calls) == 3
