"""The ledger on the append-only ``ledger`` branch (ruling D5, amendment A-67).

The branch holds one file, ``ledger.jsonl`` (``kintsugi.proof.ledger`` format, h_0 from T0 and
the T0 public key). Each append is one commit whose parent is the previous tip, written with git
plumbing (``hash-object``, ``mktree``, ``commit-tree``) and moved with a compare-and-swap
``update-ref``, so two writers can never fork it and no working tree is touched. The branch is
not a protected branch: its commits carry no session and are outside C2–C5. Its integrity is
the hash chain itself plus the hourly signed ``anchor-*`` tags (C1), which make a rewrite of an
anchored entry visible to anyone holding a published anchor. Pushes are fast-forward only.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kintsugi.proof.anchor import TAG_PREFIX, Anchor, sign_anchor, tag_name
from kintsugi.proof.attest import Signer
from kintsugi.proof.ledger import Ledger
from kintsugi.sandbox import proc

LEDGER_FILE = "ledger.jsonl"
ZERO = "0" * 40


class LedgerBranchError(RuntimeError):
    pass


class BranchLedger:
    """Same port as ``crew.factory.FileLedger`` (``append``, ``entries``), plus anchors."""

    def __init__(self, repo: Path, genesis: str, *, branch: str = "ledger") -> None:
        self.repo = Path(repo)
        self.genesis = genesis
        self.ref = f"refs/heads/{branch}"
        self._lock = threading.Lock()

    def _git(self, *args: str, input_text: str | None = None, check: bool = True) -> str:
        result = proc.run(["git", "-C", os.fspath(self.repo), *args], input_text=input_text,
                          timeout_s=60)  # fmt: skip
        if check and not result.ok:
            raise LedgerBranchError(f"git {args[0]} failed: {result.stderr.strip()[-300:]}")
        return result.stdout.strip() if result.ok else ""

    def tip(self) -> str | None:
        return self._git("rev-parse", "--verify", "-q", self.ref, check=False) or None

    def _ledger(self, tip: str | None) -> Ledger:
        text = self._git("show", f"{tip}:{LEDGER_FILE}") + "\n" if tip else ""
        return Ledger.from_jsonl(text, self.genesis)

    def entries(self) -> tuple[dict[str, Any], ...]:
        return self._ledger(self.tip()).entries

    def append(self, entry: Mapping[str, Any], *, at: datetime | None = None) -> str:
        """Append one entry as one commit; returns h_i."""
        when = (at or datetime.now(UTC)).isoformat(timespec="seconds")
        with self._lock:
            for _attempt in range(3):
                tip = self.tip()
                ledger = self._ledger(tip)
                record = ledger.append({"at": when, **dict(entry)})
                blob = self._git("hash-object", "-w", "--no-filters", "--stdin",
                                 input_text=ledger.to_jsonl())  # fmt: skip
                tree = self._git("mktree", input_text=f"100644 blob {blob}\t{LEDGER_FILE}\n")
                parents = ["-p", tip] if tip else []
                subject = f"ledger: seq {record['seq']} {record.get('type', '')}".rstrip()
                sha = self._git("commit-tree", tree, *parents, "-m", subject)
                moved = proc.run(["git", "-C", os.fspath(self.repo), "update-ref", self.ref, sha,
                                  tip or ZERO], timeout_s=60)  # fmt: skip
                if moved.ok:
                    return str(record["hash"])
            raise LedgerBranchError("the ledger branch kept moving under this writer")

    def anchors(self) -> list[Anchor]:
        out = self._git("for-each-ref", f"refs/tags/{TAG_PREFIX}*",
                        "--format=%(refname:short)%00%(contents)%01")  # fmt: skip
        found = []
        for record in out.split("\x01"):
            tag, _, message = record.strip().partition("\x00")
            if tag:
                found.append(Anchor.from_message(tag, message))
        return sorted(found, key=lambda a: a.seq)

    def anchor(self, signer: Signer, *, at: datetime | None = None) -> Anchor:
        """Tag the current head for the hour containing ``at``; the same hour twice is a no-op
        that returns the existing anchor (an activity retry must not re-sign)."""
        tag = tag_name(at or datetime.now(UTC))
        existing = [a for a in self.anchors() if a.tag == tag]
        if existing:
            return existing[0]
        with self._lock:
            tip = self.tip()
            if tip is None:
                raise LedgerBranchError("nothing to anchor: the ledger branch has no entry yet")
            ledger = self._ledger(tip)
            anchor = sign_anchor(tag, len(ledger), ledger.head, signer)
            self._git("tag", "-a", tag, "-m", anchor.message(), tip)
            return anchor

    def push(self, remote: str = "origin") -> None:
        """Fast-forward only: a rejected push means someone rewrote the remote ledger."""
        tags = f"refs/tags/{TAG_PREFIX}*"
        self._git("push", remote, f"{self.ref}:{self.ref}", f"{tags}:{tags}")
