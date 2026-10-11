"""Orchestrator commits and the autonomy checks C2–C5 (spec §4.5, §7.5; plan §2a, §2b, §2d).

**Commit protocol** (:func:`orchestrator_commit`), run after a headless session exits:

1. stage everything in the worktree; refuse protected-path violations and an over-budget diff;
2. compute the tree SHA (``git write-tree``) and sign the attestation over it;
3. commit with the ``Kintsugi-*`` trailers under ``ENTIRE_TEST_TTY=0`` (the gateway forces it);
   Entire's ``prepare-commit-msg`` hook appends ``Entire-Checkpoint:``;
4. verify the commit kept the signed tree and parent, carries no forbidden trailer, and names a
   checkpoint whose ref exists. Otherwise the commit is undone (``reset --soft``) and refused,
   so an unlinked commit is never pushed.

**Checks** (:func:`verify_commit`), usable online or offline from the refs alone:

- C2 every ``Kintsugi-*`` trailer is present and no ``Co-authored-by:``/``Claude-Session:``;
- C3 the signature verifies over (nonce, tree, parent, role, incident, prompt hash);
- C4 the named checkpoint exists, its stored transcript is intact (``content_hash``), contains
  the nonce verbatim, and its ``files_touched`` covers the commit's changed files;
- C5 the first user prompt of the session hashes to ``Kintsugi-Prompt``.

``Kintsugi-Prompt`` is ``sha256`` of the exact UTF-8 bytes of the session's first prompt; the
attestation's ``prompt_template_sha`` field carries that same digest (Amendments log).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from kintsugi.gates.protected_paths import Policy
from kintsugi.memory import entire
from kintsugi.proof import attest
from kintsugi.sandbox import proc

FORBIDDEN_TRAILERS = ("Co-authored-by", "Claude-Session")
_FORBIDDEN = re.compile(r"^\s*(co-authored-by|claude-session)\s*:", re.IGNORECASE | re.MULTILINE)
_ULID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


class CommitRefused(RuntimeError):
    pass


class CheckpointMissing(CommitRefused):
    """The commit did not get a linked Entire checkpoint; it was undone."""


def prompt_digest(prompt: str) -> str:
    return "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def message_problems(
    message: str, *, require_attestation: bool, require_checkpoint: bool
) -> list[str]:
    problems: list[str] = []
    if _FORBIDDEN.search(message):
        problems.append(f"forbidden trailer present (one of {', '.join(FORBIDDEN_TRAILERS)})")
    trailers = attest.parse_trailers(message)
    if require_attestation:
        missing = [key for key in attest.TRAILER_KEYS if len(trailers.get(key, [])) != 1]
        if missing:
            problems.append(f"missing or repeated attestation trailers: {missing}")
    if require_checkpoint:
        ids = trailers.get(attest.ENTIRE_TRAILER, [])
        if len(ids) != 1 or not _ULID.match(ids[0]):
            problems.append(f"expected one valid {attest.ENTIRE_TRAILER} trailer, got {ids}")
    return problems


def _git(repo: Path, *args: str, input_text: str | None = None, timeout_s: float = 120.0) -> str:
    result = proc.run(["git", "-C", str(repo), *args], input_text=input_text, timeout_s=timeout_s)
    if not result.ok:
        detail = (result.stderr or result.stdout).strip()[-800:]
        raise CommitRefused(f"git {args[0]} failed: {detail}")
    return result.stdout


@dataclass(frozen=True)
class CommitOutcome:
    sha: str
    tree_sha: str
    parent_sha: str
    checkpoint_id: str | None
    attestation: attest.SessionAttestation
    changed_paths: tuple[str, ...]
    changed_lines: int


def _staged_paths(repo: Path) -> list[str]:
    return [p for p in _git(repo, "diff", "--cached", "--name-only", "-z").split("\0") if p]


def _staged_lines(repo: Path) -> int:
    total = 0
    for line in _git(repo, "diff", "--cached", "--numstat").splitlines():
        added, deleted, _ = line.split("\t", 2)
        total += (int(added) if added.isdigit() else 0) + (int(deleted) if deleted.isdigit() else 0)
    return total


def orchestrator_commit(
    worktree: Path,
    *,
    attestation: attest.SessionAttestation,
    signer: attest.Signer,
    subject: str,
    policy: Policy,
    scopes: Iterable[str] = ("surgeon",),
    body: str = "",
    approved_large_diff: bool = False,
    require_checkpoint: bool = True,
    session_paths: Iterable[str] | None = None,
    timeout_s: float = 120.0,
) -> CommitOutcome:
    """``session_paths``: files the session's Write/Edit tool calls touched (see
    :func:`tool_edited_paths`). Any other staged change is refused before committing, because
    Entire's checkpoint would not record it and audit check C4 would fail later."""
    worktree = Path(worktree)
    _git(worktree, "add", "-A")
    paths = _staged_paths(worktree)
    if not paths:
        raise CommitRefused("the session changed nothing")
    if session_paths is not None:
        unexplained = sorted(set(paths) - set(session_paths))
        if unexplained:
            _git(worktree, "reset", "-q")
            raise CommitRefused(f"changes the session's tool calls do not explain: {unexplained}")
    violations = policy.violations(paths, list(scopes))
    if violations:
        _git(worktree, "reset", "-q")
        raise CommitRefused(f"protected paths touched: {[(v.path, v.pattern) for v in violations]}")
    lines = _staged_lines(worktree)
    if policy.over_budget(lines) and not approved_large_diff:
        _git(worktree, "reset", "-q")
        budget = policy.diff_budget_lines
        raise CommitRefused(f"diff of {lines} lines exceeds the {budget}-line budget")
    tree = _git(worktree, "write-tree").strip()
    parent = _git(worktree, "rev-parse", "HEAD").strip()
    signed = attest.attest(attestation, signer, tree_sha=tree, parent_sha=parent)
    message = subject.strip() + "\n\n"
    message += (body.strip() + "\n\n") if body.strip() else ""
    message += attest.format_trailers(attest.trailers(signed)) + "\n"
    problems = message_problems(message, require_attestation=True, require_checkpoint=False)
    if problems:
        raise CommitRefused("; ".join(problems))
    _git(worktree, "commit", "-q", "-F", "-", input_text=message, timeout_s=timeout_s)
    sha = _git(worktree, "rev-parse", "HEAD").strip()
    final = _git(worktree, "log", "-1", "--format=%B", sha)
    problems = []
    if _git(worktree, "rev-parse", f"{sha}^{{tree}}").strip() != tree:
        problems.append("tree changed after signing")
    if _git(worktree, "rev-parse", f"{sha}^").strip() != parent:
        problems.append("parent changed after signing")
    problems += message_problems(
        final, require_attestation=True, require_checkpoint=require_checkpoint
    )
    checkpoint_id = (attest.parse_trailers(final).get(attest.ENTIRE_TRAILER) or [None])[0]
    if require_checkpoint and checkpoint_id and not _ref_exists(worktree, checkpoint_id):
        problems.append(f"checkpoint ref for {checkpoint_id} does not exist")
    if problems:
        _git(worktree, "reset", "-q", "--soft", parent)
        raise CheckpointMissing("; ".join(problems))
    return CommitOutcome(sha, tree, parent, checkpoint_id, signed, tuple(paths), lines)


def _ref_exists(repo: Path, checkpoint_id: str) -> bool:
    ref = entire.checkpoint_ref(checkpoint_id)
    result = proc.run(["git", "-C", str(repo), "show-ref", "--verify", "-q", ref], timeout_s=30)
    return result.returncode == 0


_EDIT_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})


def tool_edited_paths(tool_uses: Iterable[dict[str, object]], worktree: Path) -> set[str]:
    """Repository-relative POSIX paths a session's file-editing tool calls named."""
    root = Path(worktree).resolve()
    paths: set[str] = set()
    for use in tool_uses:
        if use.get("name") not in _EDIT_TOOLS:
            continue
        tool_input = use.get("input")
        raw = tool_input.get("file_path") if isinstance(tool_input, dict) else None
        if not isinstance(raw, str):
            continue
        candidate = Path(raw)
        absolute = candidate if candidate.is_absolute() else root / candidate
        try:
            paths.add(absolute.resolve().relative_to(root).as_posix())
        except ValueError:
            continue  # outside the worktree: never part of this commit
    return paths


# ---- C2–C5 ----------------------------------------------------------------------------------


def first_user_prompt(transcript: bytes) -> str | None:
    """The first real user prompt in a Claude Code native (``full.jsonl``) transcript."""
    for raw in transcript.splitlines():
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "user" or entry.get("isMeta"):
            continue
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]
            texts = [str(b.get("text", "")) for b in blocks]
            if texts:
                return "".join(texts)
    return None


def check_c4(
    stored: entire.StoredCheckpoint, nonce: str, changed_paths: Iterable[str]
) -> list[str]:
    problems: list[str] = []
    if not stored.sessions:
        return ["checkpoint has no sessions"]
    if not all(session.intact for session in stored.sessions):
        problems.append("a stored transcript does not match its content_hash")
    if not any(nonce.encode("utf-8") in session.full for session in stored.sessions):
        problems.append("the attested nonce is not in any session transcript")
    untouched = sorted(set(changed_paths) - set(stored.files_touched))
    if untouched:
        problems.append(f"changed files the checkpoint does not record: {untouched}")
    return problems


def check_c5(stored: entire.StoredCheckpoint, expected_prompt_digest: str) -> list[str]:
    for session in stored.sessions:
        prompt = first_user_prompt(session.full)
        if prompt is not None and prompt_digest(prompt) == expected_prompt_digest:
            return []
    return ["no session's first prompt hashes to Kintsugi-Prompt"]


@dataclass(frozen=True)
class LinkageReport:
    commit: str
    checkpoint_id: str | None
    problems: dict[str, list[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not any(self.problems.values())


def verify_commit(repo: Path, sha: str, *, public_key: Ed25519PublicKey) -> LinkageReport:
    """Checks C2–C5 for one commit, from git and the checkpoint refs alone."""
    repo = Path(repo)
    message = _git(repo, "log", "-1", "--format=%B", sha)
    trailers = attest.parse_trailers(message)
    problems: dict[str, list[str]] = {
        "C2": message_problems(message, require_attestation=True, require_checkpoint=True)
    }
    checkpoint_id = (trailers.get(attest.ENTIRE_TRAILER) or [None])[0]
    if problems["C2"]:
        return LinkageReport(sha, checkpoint_id, problems)
    one = {key: values[0] for key, values in trailers.items()}
    claimed = attest.SessionAttestation(
        nonce=one["Kintsugi-Nonce"], role=one["Kintsugi-Role"], incident=one["Kintsugi-Incident"],
        worktree="", base_sha="", prompt_template_sha=one["Kintsugi-Prompt"], t_start="",
        tree_sha=_git(repo, "rev-parse", f"{sha}^{{tree}}").strip(),
        parent_sha=_git(repo, "rev-parse", f"{sha}^").strip(),
        signature=one["Kintsugi-Attestation"],
    )  # fmt: skip
    problems["C3"] = [] if attest.verify_attestation(claimed, public_key) else ["bad signature"]
    try:
        stored = entire.read_checkpoint(repo, str(checkpoint_id))
    except entire.EntireError as error:
        problems["C4"] = [str(error)]
        problems["C5"] = ["no checkpoint to read the prompt from"]
        return LinkageReport(sha, checkpoint_id, problems)
    changed = [p for p in _git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", sha)
               .split("\0") if p]  # fmt: skip
    problems["C4"] = check_c4(stored, claimed.nonce, changed)
    problems["C5"] = check_c5(stored, claimed.prompt_template_sha)
    return LinkageReport(sha, checkpoint_id, problems)


def why_agrees(repo: Path, sha: str, checkpoint_id: str, *, samples: int = 3) -> list[str]:
    """Cross-check with ``entire why``: sampled added lines resolve to this commit and its
    checkpoint."""
    problems: list[str] = []
    diff = _git(Path(repo), "show", "--format=", "--unified=0", sha)
    targets: list[tuple[str, int]] = []
    path = None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("@@") and path is not None:
            match = re.search(r"\+(\d+)", line)
            if match:
                targets.append((path, int(match.group(1))))
    for path_, number in targets[:samples]:
        found = entire.why(repo, path_, number)
        if found.checkpoint_id != checkpoint_id or not found.commit_sha.startswith(sha[:7]):
            problems.append(f"{path_}:{number} → {found.checkpoint_id} @ {found.commit_sha[:8]}")
    return problems


def strip_nonce(stored: entire.StoredCheckpoint, nonce: str) -> entire.StoredCheckpoint:
    """A mutated copy without the nonce (keeps content_hash): the negative control for C4."""
    sessions = [replace(s, full=s.full.replace(nonce.encode(), b"")) for s in stored.sessions]
    return replace(stored, sessions=sessions)
