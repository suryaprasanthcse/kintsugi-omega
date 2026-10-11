"""Synthetic Entire checkpoint refs with the exact on-disk layout of Entire 0.11.4.

Layout verified on real checkpoints: ``metadata.json`` with a ``sessions`` index, and per
session ``0/metadata.json``, ``0/full.jsonl`` (native transcript), ``0/transcript.jsonl``
(compact) and ``0/content_hash.txt`` = ``sha256(0/full.jsonl)``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

from kintsugi.memory import entire
from kintsugi.sandbox import proc


def git(repo: Path, *args: str, input_text: str | None = None) -> str:
    result = proc.run(["git", "-C", str(repo), *args], timeout_s=60, input_text=input_text)
    assert result.ok, result.stderr
    return result.stdout.strip()


def blob(repo: Path, content: bytes) -> str:
    path = repo / ".git" / "kintsugi-blob.tmp"
    path.write_bytes(content)
    sha = git(repo, "hash-object", "-w", str(path))
    path.unlink()
    return sha


def user_line(prompt: str) -> bytes:
    """A native Claude Code transcript entry for a user prompt."""
    entry = {"type": "user", "message": {"role": "user", "content": prompt}}
    return json.dumps(entry).encode() + b"\n"


def write_checkpoint(
    repo: Path,
    checkpoint_id: str,
    full: bytes,
    *,
    files_touched: Sequence[str] = ("attempt.txt",),
    session_id: str = "sess-1",
    tamper: bool = False,
    finalize: bool = False,
) -> str:
    session_meta = {"checkpoint_id": checkpoint_id, "session_id": session_id, "turn_id": "t1"}
    top = {
        "checkpoint_id": checkpoint_id,
        "strategy": "manual-commit",
        "files_touched": list(files_touched),
        "sessions": [
            {
                "metadata": "/0/metadata.json",
                "transcript": "/0/full.jsonl",
                "compact_transcript": "/0/transcript.jsonl",
                "content_hash": "/0/content_hash.txt",
                "prompt": "",
            }
        ],
    }
    recorded = hashlib.sha256(full).hexdigest()
    stored = full + b'{"tampered": true}\n' if tamper else full
    entries = {
        "content_hash.txt": blob(repo, f"sha256:{recorded}".encode()),
        "full.jsonl": blob(repo, stored),
        "metadata.json": blob(repo, json.dumps(session_meta).encode()),
        "transcript.jsonl": blob(repo, b'{"type":"user","content":"x"}\n'),
    }
    inner = "".join(f"100644 blob {sha}\t{name}\n" for name, sha in sorted(entries.items()))
    subtree = git(repo, "mktree", input_text=inner)
    top_blob = blob(repo, json.dumps(top).encode())
    listing = f"040000 tree {subtree}\t0\n100644 blob {top_blob}\tmetadata.json\n"
    root = git(repo, "mktree", input_text=listing)
    ref = entire.checkpoint_ref(checkpoint_id)
    parent = ["-p", git(repo, "rev-parse", ref)] if finalize else []
    subject = "Finalize transcript for Checkpoint" if finalize else "Checkpoint"
    commit = git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                 "commit-tree", root, *parent, "-m", f"{subject}: {checkpoint_id}")  # fmt: skip
    git(repo, "update-ref", ref, commit)
    return commit
