"""Archiving a senses store's rehearsal history before a real Sentinel run."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from kintsugi.senses.store import HitEvent, SenseStore, hit_key


def _claim(store: SenseStore, trace: str) -> str:
    key = hit_key("det-1", trace, "verdict")
    store.claim_hit(HitEvent(key, "det-1", trace, "verdict", "poller", receptor="R-007",
                             host="ledger", detected_at="2026-10-11T10:00:00Z"))  # fmt: skip
    return key


def test_archive_keeps_the_evidence_and_clears_stand_in_dispositions(tmp_path: Path) -> None:
    store = SenseStore(tmp_path / "senses.sqlite")
    first = _claim(store, "t1")
    _claim(store, "t2")
    store.assign(first, "INC-REH-0001", "opened")
    store.set_watermark("det-1", "2026-10-11T10:00:00Z")
    assert store.claim_event("evt-1", "alert.per_event_match", "sha")

    counts = store.archive(tmp_path / "archive" / "stage4.sqlite")

    assert counts == {"hits": 2, "queued": 2}
    assert store.seen_hits() == {}
    assert store.pending() == 0
    assert store.watermark("det-1") == "2026-10-11T10:00:00Z"  # no re-sweep of the archive
    assert not store.claim_event("evt-1", "alert.per_event_match", "sha")  # still a dup
    with sqlite3.connect(tmp_path / "archive" / "stage4.sqlite") as db:
        rows = db.execute("SELECT hit_key, incident FROM seen_hits ORDER BY hit_key").fetchall()
    assert (first, "INC-REH-0001") in rows
    assert len(rows) == 2


def test_archives_are_never_overwritten(tmp_path: Path) -> None:
    store = SenseStore(tmp_path / "senses.sqlite")
    dest = tmp_path / "a.sqlite"
    store.archive(dest)
    with pytest.raises(FileExistsError):
        store.archive(dest)
