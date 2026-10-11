"""Entire 0.11.4 adapter: checkpoints, provenance, sessions and checkpoint refs.

Verified against the installed CLI (docs/EXECUTION_PLAN.md §1, §2b):

- Checkpoints are one git ref each: ``refs/entire/checkpoints/<last two chars>/<ULID>``. A ref's
  tree holds ``metadata.json`` and, per session ``i``, ``i/metadata.json``, ``i/full.jsonl`` (the
  agent's native transcript), ``i/transcript.jsonl`` (Entire's compact form) and
  ``i/content_hash.txt`` = ``sha256(i/full.jsonl)``. :func:`read_checkpoint` reads them with plain
  git, so offline audits need no Entire binary.
- ``entire checkpoint explain|list``, ``entire why`` and ``entire session current|info|list``
  have ``--json``; ``entire version`` prints text. ``entire doctor`` is an interactive fixer and
  is deliberately not wrapped: health comes from ``entire graph doctor --json``.
- The pre-push hook pushes only refs in its local queue; every Stop hook adds a "Finalize
  transcript" commit that moves a ref again. :func:`sync_checkpoint_refs` therefore compares
  with the remote and treats the remote state, not git's exit code, as the truth.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from kintsugi import PINNED_TOOLCHAIN
from kintsugi.sandbox import proc

PathLike = str | os.PathLike[str]
CHECKPOINT_REF_PREFIX = "refs/entire/checkpoints/"
_SESSION_FILES = ("metadata", "transcript", "compact_transcript", "content_hash")
_ULID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_VERSION = re.compile(r"^Entire CLI (\S+)", re.MULTILINE)


class EntireError(RuntimeError):
    pass


class EntireSchemaError(EntireError):
    """Entire's JSON no longer has the shape this adapter was pinned against."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="allow")  # new fields are fine; missing required ones are not


class CheckpointSummary(_Strict):
    checkpoint_id: str
    session_id: str
    agent: str
    date: str
    message: str
    session_ids: list[str]


class ExplainSession(_Strict):
    session_id: str
    agent: str
    model: str | None = None
    created_at: str
    turn_id: str | None = None
    files_touched: list[str] = []


class CheckpointExplain(_Strict):
    checkpoint_id: str
    strategy: str
    branch: str
    files_touched: list[str]
    sessions: list[ExplainSession]


class WhyLine(_Strict):
    line_number: int
    authorship: str
    commit_sha: str
    checkpoint_id: str | None = None
    session_id: str | None = None
    agent: str | None = None
    model: str | None = None
    prompt: str | None = None


class SessionEnvelope(_Strict):
    session_id: str
    agent: str
    status: str
    worktree_path: str
    turns: int
    checkpoints: int


def _run(args: list[str], repo: PathLike | None, *, timeout_s: float = 120.0) -> str:
    result = proc.run(["entire", *args], cwd=repo, timeout_s=timeout_s)
    if not result.ok:
        detail = (result.stderr or result.stdout).strip()[-1500:]
        raise EntireError(f"entire {' '.join(args[:2])} failed ({result.returncode}): {detail}")
    return result.stdout


def _parse[T: BaseModel](model: type[T], data: Any, what: str) -> T:
    try:
        return model.model_validate(data)
    except ValidationError as error:
        raise EntireSchemaError(f"{what}: {error}") from None


def _json(args: list[str], repo: PathLike | None) -> Any:
    text = _run(args, repo)
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise EntireSchemaError(f"expected JSON from entire {args[0]}: {error}") from None


# ---- CLI ------------------------------------------------------------------------------------


def version() -> str:
    match = _VERSION.search(_run(["version"], None))
    if match is None:
        raise EntireSchemaError("entire version: unrecognised output")
    return match.group(1)


def require_pinned() -> None:
    found = version()
    if found != PINNED_TOOLCHAIN["entire"]:
        raise EntireError(f"Entire {found} is not the pinned {PINNED_TOOLCHAIN['entire']}")


def enable(repo: PathLike, *, agent: str = "claude-code") -> None:
    """Install Entire's agent and git hooks non-interactively (onboarding step O4)."""
    _run(["enable", "--agent", agent, "--telemetry=false"], repo, timeout_s=180)


def status(repo: PathLike) -> str:
    return _run(["status"], repo)


def checkpoint_list(repo: PathLike) -> list[CheckpointSummary]:
    data = _json(["checkpoint", "list", "--json", "--no-pager"], repo)
    return [_parse(CheckpointSummary, item, "checkpoint list") for item in data]


def checkpoint_explain(repo: PathLike, checkpoint_id: str) -> CheckpointExplain:
    data = _json(["checkpoint", "explain", "--checkpoint", checkpoint_id, "--json"], repo)
    return _parse(CheckpointExplain, data, "checkpoint explain")


def checkpoint_transcript(repo: PathLike, checkpoint_id: str) -> str:
    return _run(["checkpoint", "explain", "--checkpoint", checkpoint_id, "--transcript"], repo)


def why(repo: PathLike, path: str, line: int) -> WhyLine:
    data = _json(["why", f"{path}:{line}", "--json"], repo)
    if not isinstance(data, dict) or "line" not in data:
        raise EntireSchemaError("entire why: no 'line' object")
    return _parse(WhyLine, data["line"], "why")


def session_current(repo: PathLike) -> SessionEnvelope:
    return _parse(SessionEnvelope, _json(["session", "current", "--json"], repo), "session")


def session_info(repo: PathLike, session_id: str) -> SessionEnvelope:
    data = _json(["session", "info", session_id, "--json"], repo)
    return _parse(SessionEnvelope, data, "session info")


def session_list(repo: PathLike) -> list[SessionEnvelope]:
    sessions = _json(["session", "list", "--json"], repo)
    return [_parse(SessionEnvelope, s, "session list") for s in sessions]


def session_stop(repo: PathLike, session_id: str) -> None:
    """Mark a session ended without condensing anything (L2: abandoned attempts)."""
    _run(["session", "stop", session_id, "--force"], repo)


def session_resume(repo: PathLike, branch: str) -> str:
    """Check out ``branch`` and restore its session log (L2 in-place recovery)."""
    return _run(["session", "resume", branch, "--force"], repo, timeout_s=300)


# ---- checkpoint refs (plain git: works offline) ----------------------------------------------


def checkpoint_ref(checkpoint_id: str) -> str:
    if not _ULID.match(checkpoint_id):
        raise ValueError(f"not a checkpoint id: {checkpoint_id!r}")
    return f"{CHECKPOINT_REF_PREFIX}{checkpoint_id[-2:]}/{checkpoint_id}"


def _git(repo: PathLike, *args: str, timeout_s: float = 120.0) -> str:
    return proc.run(["git", "-C", os.fspath(repo), *args], timeout_s=timeout_s, check=True).stdout


def local_checkpoint_refs(repo: PathLike) -> dict[str, str]:
    out = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)", CHECKPOINT_REF_PREFIX)
    return dict(line.split(" ", 1) for line in out.splitlines() if line.strip())


def remote_checkpoint_refs(repo: PathLike, remote: str = "origin") -> dict[str, str]:
    out = _git(repo, "ls-remote", remote, f"{CHECKPOINT_REF_PREFIX}*", timeout_s=60)
    return {ref: sha for sha, ref in (line.split("\t", 1) for line in out.splitlines() if line)}


@dataclass(frozen=True)
class SyncReport:
    pushed: tuple[str, ...]
    out_of_sync: tuple[str, ...]

    @property
    def in_sync(self) -> bool:
        return not self.out_of_sync


def sync_checkpoint_refs(repo: PathLike, remote: str = "origin") -> SyncReport:
    """Push checkpoint refs that differ from ``remote``, then judge by the remote's final state.

    Entire's pre-push hook may push the same refs concurrently, which makes git report
    "cannot lock ref … is at X but expected Y" even though X is exactly what we wanted.
    """
    local = local_checkpoint_refs(repo)
    before = remote_checkpoint_refs(repo, remote)
    stale = sorted(ref for ref, sha in local.items() if before.get(ref) != sha)
    if stale:
        proc.run(["git", "-C", os.fspath(repo), "push", remote, *[f"{r}:{r}" for r in stale]],
                 timeout_s=300)  # fmt: skip
    after = remote_checkpoint_refs(repo, remote)
    behind = tuple(sorted(ref for ref, sha in local.items() if after.get(ref) != sha))
    return SyncReport(pushed=tuple(stale), out_of_sync=behind)


@dataclass(frozen=True)
class StoredSession:
    index: int
    metadata: dict[str, Any]
    full: bytes
    compact: bytes
    content_hash: str

    @property
    def intact(self) -> bool:
        """The stored transcript still matches the hash Entire recorded at checkpoint time."""
        return self.content_hash == "sha256:" + hashlib.sha256(self.full).hexdigest()

    @property
    def session_id(self) -> str:
        return str(self.metadata["session_id"])


@dataclass(frozen=True)
class StoredCheckpoint:
    checkpoint_id: str
    ref: str
    metadata: dict[str, Any]
    sessions: list[StoredSession] = field(default_factory=list)

    @property
    def files_touched(self) -> list[str]:
        return list(self.metadata.get("files_touched", []))


def read_checkpoint(repo: PathLike, checkpoint_id: str) -> StoredCheckpoint:
    """Read a checkpoint straight from its ref, exactly as Entire stored it."""
    ref = checkpoint_ref(checkpoint_id)

    def show(path: str) -> bytes:
        return proc.run_binary(["git", "-C", os.fspath(repo), "show", f"{ref}:{path}"])

    try:
        metadata = json.loads(show("metadata.json"))
    except proc.ProcError as error:
        raise EntireError(f"checkpoint {checkpoint_id} has no ref {ref} here") from error
    if metadata.get("checkpoint_id") != checkpoint_id:
        raise EntireSchemaError(f"{ref}: metadata names {metadata.get('checkpoint_id')!r}")
    sessions = []
    for index, entry in enumerate(metadata.get("sessions", [])):
        try:
            paths = {key: entry[key].lstrip("/") for key in _SESSION_FILES}
        except KeyError as error:
            raise EntireSchemaError(f"{ref}: session entry lacks {error}") from None
        sessions.append(
            StoredSession(
                index=index,
                metadata=json.loads(show(paths["metadata"])),
                full=show(paths["transcript"]),
                compact=show(paths["compact_transcript"]),
                content_hash=show(paths["content_hash"]).decode("ascii").strip(),
            )
        )
    return StoredCheckpoint(checkpoint_id, ref, metadata, sessions)
