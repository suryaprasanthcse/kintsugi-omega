"""Hash-chained incident ledger (spec §7.3, Appendix E.2).

    h_0 = SHA-256("kintsugi-omega-genesis" ‖ T0 ‖ attest_pubkey)
    h_i = SHA-256(JCS(entry_i without "hash") ‖ h_{i-1})

Byte encodings: T0 is its RFC 3339 UTC string in UTF-8; the public key is its 32 raw Ed25519
bytes (or the DER SubjectPublicKeyInfo of a KMS key); h_{i-1} enters as its 32 raw digest
bytes. Hashes are stored as 64 lowercase hex characters. Every entry carries ``seq``
(1-based), ``prev`` (h_{i-1}) and ``hash`` (h_i), so a reader can locate a break exactly.

Verification finds the first broken ``seq``. An attacker who edits entry k and recomputes every
later hash produces a different head, which the published hourly anchors expose (audit C1).
The ledger never reads a clock: callers pass ``at``, keeping Temporal activities deterministic.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from kintsugi.proof.canonical import canonicalize

GENESIS_PREFIX = b"kintsugi-omega-genesis"
_RESERVED = ("seq", "prev", "hash")


class LedgerError(ValueError):
    pass


def genesis_hash(t0: str, attest_pubkey: bytes) -> str:
    return hashlib.sha256(GENESIS_PREFIX + t0.encode("utf-8") + attest_pubkey).hexdigest()


def entry_hash(entry: Mapping[str, Any], prev_hash: str) -> str:
    body = {key: value for key, value in entry.items() if key != "hash"}
    return hashlib.sha256(canonicalize(body) + bytes.fromhex(prev_hash)).hexdigest()


@dataclass(frozen=True)
class VerifyReport:
    ok: bool
    entries: int
    head: str
    first_bad_seq: int | None = None
    reason: str | None = None


def verify(
    entries: Sequence[Mapping[str, Any]],
    genesis: str,
    *,
    anchors: Mapping[int, str] | None = None,
) -> VerifyReport:
    """Verify the chain from h_0, then every anchor ``{seq: h_seq}`` (audit check C1)."""
    expected_prev = genesis
    for position, entry in enumerate(entries, start=1):
        seq = entry.get("seq")
        if seq != position:
            return _broken(position, expected_prev, f"expected seq {position}, found {seq!r}")
        if entry.get("prev") != expected_prev:
            return _broken(position, expected_prev, "prev does not link to the previous hash")
        if entry.get("hash") != entry_hash(entry, expected_prev):
            return _broken(position, expected_prev, "content does not match its hash")
        expected_prev = entry["hash"]
    for seq, anchored in sorted((anchors or {}).items()):
        if seq < 0 or seq > len(entries):
            return _broken(seq, expected_prev, f"anchor at seq {seq} is outside the ledger")
        actual = genesis if seq == 0 else entries[seq - 1]["hash"]
        if actual != anchored:
            return _broken(seq, expected_prev, f"anchor h_{seq} does not match the ledger")
    return VerifyReport(ok=True, entries=len(entries), head=expected_prev)


def _broken(seq: int, head: str, reason: str) -> VerifyReport:
    return VerifyReport(ok=False, entries=seq - 1, head=head, first_bad_seq=seq, reason=reason)


class Ledger:
    """An append-only, in-memory ledger with JSON Lines persistence."""

    def __init__(self, genesis: str, entries: Iterable[Mapping[str, Any]] = ()) -> None:
        self.genesis = genesis
        self._entries: list[dict[str, Any]] = [dict(entry) for entry in entries]
        report = verify(self._entries, genesis)
        if not report.ok:
            raise LedgerError(f"seq {report.first_bad_seq}: {report.reason}")

    @property
    def head(self) -> str:
        return self._entries[-1]["hash"] if self._entries else self.genesis

    @property
    def entries(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(entry) for entry in self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def append(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        reserved = [key for key in _RESERVED if key in entry]
        if reserved:
            raise LedgerError(f"the ledger assigns these fields itself: {reserved}")
        for required in ("type", "at"):
            if not entry.get(required):
                raise LedgerError(f"entry needs a non-empty {required!r}")
        record = {"seq": len(self._entries) + 1, **entry, "prev": self.head}
        record["hash"] = entry_hash(record, self.head)
        self._entries.append(record)
        return dict(record)

    def to_jsonl(self) -> str:
        return "".join(canonicalize(entry).decode("utf-8") + "\n" for entry in self._entries)

    @classmethod
    def from_jsonl(cls, text: str, genesis: str) -> Ledger:
        entries = [json.loads(line) for line in text.splitlines() if line.strip()]
        return cls(genesis, entries)
