from __future__ import annotations

import copy
import hashlib
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kintsugi.proof.canonical import canonicalize
from kintsugi.proof.ledger import Ledger, LedgerError, entry_hash, genesis_hash, verify

T0 = "2026-10-12T00:00:00Z"
PUBKEY = bytes(range(32))
GENESIS = genesis_hash(T0, PUBKEY)


def _ledger(n: int) -> Ledger:
    ledger = Ledger(GENESIS)
    for i in range(n):
        ledger.append({"type": "incident.opened", "at": f"T0+00:{i:02d}", "incident": f"INC-{i}"})
    return ledger


def test_genesis_follows_the_spec_formula() -> None:
    expected = hashlib.sha256(b"kintsugi-omega-genesis" + T0.encode() + PUBKEY).hexdigest()
    assert expected == GENESIS


def test_entry_hash_chains_on_the_raw_previous_digest() -> None:
    ledger = _ledger(1)
    entry = ledger.entries[0]
    body = {k: v for k, v in entry.items() if k != "hash"}
    assert entry["hash"] == hashlib.sha256(canonicalize(body) + bytes.fromhex(GENESIS)).hexdigest()
    assert entry["prev"] == GENESIS
    assert entry["seq"] == 1


def test_a_clean_ledger_verifies_and_reports_its_head() -> None:
    ledger = _ledger(5)
    report = verify(ledger.entries, GENESIS)
    assert report.ok
    assert report.entries == 5
    assert report.head == ledger.head == ledger.entries[-1]["hash"]


def test_empty_ledger_head_is_genesis() -> None:
    assert Ledger(GENESIS).head == GENESIS
    assert verify([], GENESIS).ok


@pytest.mark.parametrize("seq", [1, 3, 5])
def test_content_tampering_is_located_at_the_exact_seq(seq: int) -> None:
    entries = [dict(entry) for entry in _ledger(5).entries]
    entries[seq - 1]["incident"] = "INC-FORGED"
    report = verify(entries, GENESIS)
    assert not report.ok
    assert report.first_bad_seq == seq
    assert report.reason == "content does not match its hash"


def test_deleting_an_entry_is_located() -> None:
    entries = list(_ledger(5).entries)
    del entries[2]
    report = verify(entries, GENESIS)
    assert report.first_bad_seq == 3


def test_reordering_is_located() -> None:
    entries = list(_ledger(5).entries)
    entries[1], entries[2] = entries[2], entries[1]
    assert verify(entries, GENESIS).first_bad_seq == 2


def test_wrong_genesis_breaks_the_first_link() -> None:
    other = genesis_hash("2026-10-12T00:00:01Z", PUBKEY)
    report = verify(_ledger(3).entries, other)
    assert report.first_bad_seq == 1
    assert report.reason == "prev does not link to the previous hash"


def _rechain(entries: list[dict[str, Any]], start: int) -> None:
    prev = GENESIS if start == 1 else entries[start - 2]["hash"]
    for entry in entries[start - 1 :]:
        entry["prev"] = prev
        entry["hash"] = entry_hash(entry, prev)
        prev = entry["hash"]


def test_a_rechained_forgery_is_caught_by_anchors() -> None:
    original = _ledger(6)
    anchors = {4: original.entries[3]["hash"], 6: original.head}
    forged = [dict(entry) for entry in original.entries]
    forged[2]["incident"] = "INC-FORGED"
    _rechain(forged, start=3)
    assert verify(forged, GENESIS).ok  # internally consistent …
    report = verify(forged, GENESIS, anchors=anchors)  # … but not against the published anchors
    assert not report.ok
    assert report.first_bad_seq == 4
    assert verify(original.entries, GENESIS, anchors=anchors).ok


def test_anchor_outside_the_ledger_fails() -> None:
    assert not verify(_ledger(2).entries, GENESIS, anchors={9: "00" * 32}).ok


def test_append_refuses_reserved_fields_and_requires_type_and_time() -> None:
    ledger = Ledger(GENESIS)
    with pytest.raises(LedgerError, match="assigns"):
        ledger.append({"type": "x", "at": "t", "hash": "h"})
    with pytest.raises(LedgerError, match="'at'"):
        ledger.append({"type": "x"})


def test_jsonl_round_trip_is_canonical_and_lf_only() -> None:
    ledger = _ledger(4)
    text = ledger.to_jsonl()
    assert "\r" not in text
    assert text.count("\n") == 4
    assert Ledger.from_jsonl(text, GENESIS).head == ledger.head


def test_constructor_rejects_a_broken_chain() -> None:
    entries = [dict(entry) for entry in _ledger(3).entries]
    entries[1]["type"] = "forged"
    with pytest.raises(LedgerError, match="seq 2"):
        Ledger(GENESIS, entries)


def test_entries_are_returned_as_copies() -> None:
    ledger = _ledger(2)
    snapshot = ledger.entries
    snapshot[0]["incident"] = "mutated"
    assert verify(ledger.entries, GENESIS).ok


@given(
    st.integers(min_value=1, max_value=12),
    st.data(),
)
def test_any_single_field_change_is_located(n: int, data: st.DataObject) -> None:
    entries = [dict(entry) for entry in _ledger(n).entries]
    target = data.draw(st.integers(min_value=1, max_value=n))
    field = data.draw(st.sampled_from(["type", "at", "incident"]))
    forged = copy.deepcopy(entries)
    replacement = data.draw(st.text(min_size=1).filter(lambda s: s != entries[target - 1][field]))
    forged[target - 1][field] = replacement
    assert verify(forged, GENESIS).first_bad_seq == target
