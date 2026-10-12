"""Notary sessions: commits that Omega decides but no Surgeon writes (plan §2a L3; audit C4).

Audit check C4 needs an Entire checkpoint for **every** commit inside W, and Entire links a
commit only to the Claude Code session whose Edit/Write calls made its changes (A-16). So an
auto-revert (§6.7 step 16), an eval-augment commit (step 18) and every other orchestrator-
decided change runs inside a templated headless **notary** session: the pinned ``notary``
prompt carries the operation, its nonce and the exact patch; the session applies it with
Edit/Write and runs the tests; the orchestrator then signs and commits what the session's tool
calls explain, under the protected-path scope the operation allows. When the decided result is
an exact tree (a revert restores the merge's first parent), a different tree is refused.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from kintsugi.crew import claude_session, templates
from kintsugi.crew.surgeon import session_env
from kintsugi.gates.protected_paths import load_policy
from kintsugi.proof import attest, passphrase
from kintsugi.proof.commit import CommitRefused, orchestrator_commit, prompt_digest
from kintsugi.proof.commit import tool_edited_paths as edited
from kintsugi.sandbox import proc

MODEL = "claude-haiku-5-5"
TEST_COMMAND = "python -m pytest -q"


class NotaryError(RuntimeError):
    pass


@dataclass(frozen=True)
class NotaryRequest:
    operation: str  # "revert", "eval-augment", "drill-deliver", …
    reference: str  # what it acts on, e.g. "AB-1@1.0 on scout"
    incident: str  # the incident (or VAC-/REV- record) the commit belongs to
    purpose: str  # why, in plain words; never a promise the patch contradicts (A-52)
    diff: str  # the exact change, as a unified diff
    subject: str
    body: str = ""
    scopes: tuple[str, ...] = ("surgeon",)  # protected paths the commit must not touch
    expected_tree: str | None = None  # the exact tree the result must have, when known


@dataclass(frozen=True)
class NotaryOutcome:
    sha: str
    checkpoint_id: str | None
    prompt_sha: str
    session_id: str
    cost_usd: float
    paths: tuple[str, ...]
    nonce: str


def _git(repo: Path, *args: str) -> str:
    return proc.run(["git", "-C", str(repo), *args], timeout_s=120, check=True).stdout


class Notary:
    def __init__(
        self,
        signer: attest.Signer,
        *,
        model: str = MODEL,
        max_budget_usd: float = 1.0,
        timeout_s: float = 900.0,
        run: Callable[..., claude_session.SessionResult] = claude_session.run,
        require_checkpoint: bool = True,
    ) -> None:
        self.signer = signer
        self.model = model
        self.max_budget_usd = max_budget_usd
        self.timeout_s = timeout_s
        self._run = run
        self.require_checkpoint = require_checkpoint

    def render(self, request: NotaryRequest, nonce: str) -> templates.Rendered:
        return templates.render(
            "notary",
            operation=request.operation,
            reference=request.reference,
            nonce=nonce,
            purpose=request.purpose,
            test_command=TEST_COMMAND,
            diff=request.diff.rstrip("\n"),
        )

    def commit(
        self, worktree: Path, request: NotaryRequest, *, package_root: str = "."
    ) -> NotaryOutcome:
        """Run the session in ``worktree`` (disposable) and commit its change, attested."""
        nonce = passphrase.new_nonce()
        rendered = self.render(request, nonce)
        base = _git(worktree, "rev-parse", "HEAD").strip()
        started = datetime.now(UTC).isoformat(timespec="seconds")
        session = claude_session.SessionRequest(
            prompt=rendered.prompt,
            cwd=worktree,
            allowed_tools=("Read", "Edit", "Write", "Bash(python -m pytest:*)"),
            model=self.model,
            session_id=str(uuid.uuid4()),
            max_budget_usd=self.max_budget_usd,
            timeout_s=self.timeout_s,
            extra_env=session_env(worktree / package_root),
            append_system_prompt=rendered.system,
        )
        result = self._run(session)
        if not result.ok:
            raise NotaryError(f"{request.operation}: the notary session failed: "
                              f"{result.stream.error or result.exit_code}")  # fmt: skip
        attestation = attest.SessionAttestation(
            nonce=nonce,
            role="notary",
            incident=request.incident,
            worktree=worktree.name,
            base_sha=base,
            prompt_template_sha=prompt_digest(rendered.prompt),
            t_start=started,
            hypothesis=request.operation,
        )
        try:
            outcome = orchestrator_commit(
                worktree,
                attestation=attestation,
                signer=self.signer,
                subject=request.subject[:100],
                body=request.body[:1500],
                policy=load_policy(),
                scopes=request.scopes,
                session_paths=edited(result.stream.tool_uses, worktree),
                require_checkpoint=self.require_checkpoint,
            )
        except CommitRefused as refusal:
            raise NotaryError(f"{request.operation}: {refusal}") from refusal
        if request.expected_tree is not None and outcome.tree_sha != request.expected_tree:
            _git(worktree, "reset", "-q", "--hard", base)
            raise NotaryError(
                f"{request.operation}: the session's tree {outcome.tree_sha[:12]} is not the "
                f"decided {request.expected_tree[:12]}"
            )
        return NotaryOutcome(
            outcome.sha,
            outcome.checkpoint_id,
            rendered.prompt_sha,
            result.session_id,
            result.stream.cost_usd,
            outcome.changed_paths,
            nonce,
        )
