"""`kintsugi t0 genesis`: write the window record and the ledger's genesis entry (A-67).

Run once, by the Architect, at T0, after the T0 public key is committed. It writes
``.kintsugi/window.json`` (what W is, for the audit) and appends seq 1, a ``genesis`` entry, to
the ``ledger`` branch. It neither commits ``window.json`` nor pushes: the Architect commits it
(the T0 commit, which the audit treats as W's base) and pushes ``main`` and ``ledger``.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kintsugi.proof import attest
from kintsugi.proof.audit import WINDOW_FILE, Window, manifest_digest
from kintsugi.proof.canonical import canonicalize
from kintsugi.proof.ledger_branch import BranchLedger
from kintsugi.sandbox import proc


class GenesisError(RuntimeError):
    pass


@dataclass(frozen=True)
class Genesis:
    window: Window
    genesis: str
    entry_hash: str
    path: Path


def _git(repo: Path, *args: str) -> str:
    return proc.run(["git", "-C", os.fspath(repo), *args], timeout_s=60, check=True).stdout


def t0_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def window_record(
    repo: Path,
    *,
    t0: str,
    github_repo: str,
    rulesets: Sequence[int],
    branch: str = "main",
    attest_pub: Path | None = None,
) -> dict[str, Any]:
    repo = Path(repo)
    pub_path = attest_pub or repo / ".kintsugi" / "attest.pub"
    if not pub_path.is_file():
        raise GenesisError(f"{pub_path} is missing: commit the T0 public key first (A-4)")
    pubkey = pub_path.read_text(encoding="utf-8").strip()
    attest.load_public_key(pubkey)  # must parse
    if not rulesets:
        raise GenesisError("record at least one ruleset active on main (C12)")
    base = _git(repo, "rev-parse", f"origin/{branch}").strip()
    return {
        "t0": t0, "t1": None, "repo": github_repo, "branch": branch, "base": base,
        "attest_pubkey": pubkey, "holdout_manifest": manifest_digest(repo),
        "rulesets": [int(r) for r in rulesets], "g7_check": "G7", "ledger_ref": "ledger",
    }  # fmt: skip


def write_genesis(
    repo: Path,
    *,
    github_repo: str,
    rulesets: Sequence[int],
    t0: str | None = None,
    attest_pub: Path | None = None,
) -> Genesis:
    repo = Path(repo)
    path = repo / WINDOW_FILE
    if path.exists():
        raise GenesisError(f"{WINDOW_FILE} exists: T0 happens once")
    record = window_record(repo, t0=t0 or t0_now(), github_repo=github_repo, rulesets=rulesets,
                           attest_pub=attest_pub)  # fmt: skip
    window = Window.from_json(record)
    book = BranchLedger(repo, window.genesis(), branch=window.ledger_ref)
    if book.tip() is not None:
        raise GenesisError("the ledger branch already has entries")
    body = canonicalize(record).decode("utf-8")
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")
    entry = book.append(
        {"type": "genesis", "t0": window.t0, "base": window.base,
         "window_sha256": "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(),
         "pubkey_fingerprint": attest.fingerprint(window.public_key())},
        at=window.start,
    )  # fmt: skip
    return Genesis(window, window.genesis(), entry, path)
