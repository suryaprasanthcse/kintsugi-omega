"""Stage 2 headless spikes against the real Entire 0.11.4 and Claude Code binaries.

Two tiers:

- **Hook tier** (needs only the binaries): a headless session starts, Entire's Claude Code hooks
  fire and register it. A session that is not logged in still exercises this tier, because
  SessionStart and UserPromptSubmit fire before any model call.
- **Model tier** (needs a logged-in CLI or ``ANTHROPIC_API_KEY``): the session edits a file, the
  orchestrator commits after it exits, and the commit must link to that session's checkpoint.

Run with ``KINTSUGI_CONTRACT=1 uv run pytest tests/contract -v``.
"""

from __future__ import annotations

import concurrent.futures as cf
import os
import uuid
from pathlib import Path

import pytest

from kintsugi.crew import claude_session as cs
from kintsugi.gates.protected_paths import load_policy
from kintsugi.memory import entire
from kintsugi.proof import attest, redaction
from kintsugi.proof import commit as c
from kintsugi.proof.passphrase import new_honeytoken, new_nonce
from kintsugi.sandbox import proc
from kintsugi.sandbox import worktree as wt

from .scratch import enable_entire, git, make_repo

pytestmark = pytest.mark.contract

MODEL = os.environ.get("KINTSUGI_SPIKE_MODEL", "haiku")
POLICY = load_policy()


def _logged_in() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY")) or bool(cs.auth_status().get("loggedIn"))


needs_model = pytest.mark.skipif(
    not (os.environ.get("KINTSUGI_CONTRACT") == "1" and _logged_in()),
    reason="model tier: the headless CLI is not logged in (run `claude auth login`)",
)


def _norm(path: str | Path) -> str:
    return str(path).replace("\\", "/").rstrip("/").lower()


@pytest.fixture
def host(tmp_path: Path) -> Path:
    repo = make_repo(tmp_path / "host")
    enable_entire(repo)
    return repo


def _request(cwd: Path, prompt: str, **kw: object) -> cs.SessionRequest:
    key = os.environ.get("ANTHROPIC_API_KEY")
    env = {"ANTHROPIC_API_KEY": key} if key else {}  # passed explicitly; never inherited
    return cs.SessionRequest(prompt=prompt, cwd=cwd, allowed_tools=("Write", "Edit", "Read"),
                             model=MODEL, max_budget_usd=0.50, timeout_s=300, extra_env=env,
                             **kw)  # type: ignore[arg-type]  # fmt: skip


def _task(nonce: str, name: str = "attempt.txt", extra: str = "") -> str:
    return (
        f"Kintsugi-Nonce: {nonce}\n"
        f"Create a file named {name} whose only content is the nonce above.{extra} "
        "Use the Write tool once and do nothing else."
    )


# ---- hook tier --------------------------------------------------------------------------------


def test_n4_hooks_fire_inside_a_linked_worktree(host: Path) -> None:
    attempt = wt.create(host, "INC-S", "H1")
    # Git hooks: a linked worktree resolves the common hooks directory Entire installed into.
    hooks = Path(git(attempt.path, "rev-parse", "--git-path", "hooks").strip())
    hooks = hooks if hooks.is_absolute() else attempt.path / hooks
    assert (hooks / "prepare-commit-msg").exists()
    assert _norm(hooks.resolve()) == _norm((host / ".git" / "hooks").resolve())
    # Claude Code hooks: the session registers under its own worktree path.
    request = _request(attempt.path, _task(new_nonce()))
    cs.run(request)
    sessions = {s.session_id: s for s in entire.session_list(host)}
    assert request.session_id in sessions
    assert _norm(sessions[request.session_id].worktree_path) == _norm(attempt.path.resolve())


def test_n5_concurrent_sessions_register_in_isolation(host: Path) -> None:
    attempts = [wt.create(host, "INC-S", f"H{i}") for i in (1, 2, 3)]
    requests = [_request(a.path, _task(new_nonce())) for a in attempts]
    with cf.ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(cs.run, requests))
    sessions = {s.session_id: s for s in entire.session_list(host)}
    for attempt, request in zip(attempts, requests, strict=True):
        assert _norm(sessions[request.session_id].worktree_path) == _norm(attempt.path.resolve())
    assert len({r.session_id for r in requests}) == 3


def test_a14_abandoned_attempt_mechanics(host: Path) -> None:
    attempt = wt.create(host, "INC-S", "H9")
    request = _request(attempt.path, _task(new_nonce()))
    result = cs.run(request, on_event=lambda event: event.get("type") == "system")  # kill early
    assert result.stopped_by_caller
    (attempt.path / "partial.txt").write_text("half-written by the killed session\n", "utf-8")
    listed = {s.session_id for s in entire.session_list(host)}
    if request.session_id in listed:
        entire.session_stop(host, request.session_id)
        assert entire.session_info(host, request.session_id).status != "active"
    before = set(entire.local_checkpoint_refs(host))
    wt.reset_clean(attempt)
    assert not (attempt.path / "partial.txt").exists()
    assert set(entire.local_checkpoint_refs(host)) == before  # no commit, no checkpoint
    if result.transcript_path is not None:  # the abandoned attempt's evidence
        assert result.transcript_sha256 == cs.file_sha256(result.transcript_path)


# ---- model tier -------------------------------------------------------------------------------


@needs_model
def test_a1_n2_commit_after_exit_links_and_passes_c2_to_c5(host: Path) -> None:
    attempt = wt.create(host, "INC-S", "A1")
    nonce = new_nonce()
    prompt = _task(nonce)
    result = cs.run(_request(attempt.path, prompt))
    assert result.ok, result.stream.result
    signer = attest.Ed25519Signer.generate()
    attestation = attest.SessionAttestation(
        nonce=nonce, role="surgeon", hypothesis="A1", incident="INC-S",
        worktree=str(attempt.path), base_sha=attempt.base_sha,
        prompt_template_sha=c.prompt_digest(prompt), t_start="spike",
    )  # fmt: skip
    outcome = c.orchestrator_commit(
        attempt.path, attestation=attestation, signer=signer, subject="spike: A1",
        policy=POLICY, session_paths=c.tool_edited_paths(result.stream.tool_uses, attempt.path),
    )  # fmt: skip
    assert outcome.checkpoint_id is not None
    explained = entire.checkpoint_explain(host, outcome.checkpoint_id)
    assert result.session_id in {s.session_id for s in explained.sessions}
    public = attest.load_public_key(signer.public_key_openssh())
    report = c.verify_commit(attempt.path, outcome.sha, public_key=public)
    assert report.ok, report.problems  # C2, C3, C4 (nonce verbatim) and C5 (prompt bytes exact)
    assert c.why_agrees(attempt.path, outcome.sha, outcome.checkpoint_id) == []
    stored = entire.read_checkpoint(host, outcome.checkpoint_id)
    assert c.check_c4(c.strip_nonce(stored, nonce), nonce, []) != []
    assert c.check_c5(stored, c.prompt_digest(prompt + " ")) != []


@needs_model
def test_n7_session_resume_restores_the_log_and_continues_the_same_session(host: Path) -> None:
    """Spike N7 (plan §2a L2): ``entire session resume`` + ``claude -p --resume`` in place."""
    attempt = wt.create(host, "INC-N7", "H1")
    nonce = new_nonce()
    prompt = _task(nonce, name="notes.txt")
    first = cs.run(_request(attempt.path, prompt))
    assert first.ok, first.stream.result
    signer = attest.Ed25519Signer.generate()
    attestation = attest.SessionAttestation(
        nonce=nonce, role="surgeon", hypothesis="H1", incident="INC-N7",
        worktree=str(attempt.path), base_sha=attempt.base_sha,
        prompt_template_sha=c.prompt_digest(prompt), t_start="spike",
    )  # fmt: skip
    outcome = c.orchestrator_commit(
        attempt.path, attestation=attestation, signer=signer, subject="spike: N7",
        policy=POLICY, session_paths=c.tool_edited_paths(first.stream.tool_uses, attempt.path),
    )  # fmt: skip
    assert outcome.checkpoint_id is not None
    assert first.transcript_path is not None
    parked = first.transcript_path.with_name(first.transcript_path.name + ".parked")
    first.transcript_path.rename(parked)  # a fresh machine: no local session log
    try:
        printed = entire.session_resume(attempt.path, attempt.branch)
        restored = cs.find_transcript(first.session_id)
        assert restored is not None, printed  # restored from the checkpoint
        second = cs.run(_request(
            attempt.path,
            "What exact text did you write into notes.txt earlier in this conversation? "
            "Reply with that text only and do not use any tool.",
            session_id=first.session_id, resume=True,
        ))  # fmt: skip
        assert second.ok, second.stream.result
        assert second.session_id == first.session_id  # the same session continued
        answer = second.stream.result
        text = answer.get("result", "") if isinstance(answer, dict) else str(answer or "")
        assert nonce in text  # with its context
    finally:
        if parked.exists():
            parked.unlink()


@needs_model
def test_n1_headless_honeytoken_survives_and_c6_catches_it(host: Path) -> None:
    attempt = wt.create(host, "INC-S", "N1")
    nonce, token = new_nonce(), new_honeytoken()
    prompt = _task(nonce, extra=f" Also remember this phrase: {token}.")
    result = cs.run(_request(attempt.path, prompt))
    assert result.ok
    signer = attest.Ed25519Signer.generate()
    attestation = attest.SessionAttestation(
        nonce=nonce, role="surgeon", hypothesis="N1", incident="INC-S", worktree="",
        base_sha=attempt.base_sha, prompt_template_sha=c.prompt_digest(prompt), t_start="spike",
    )  # fmt: skip
    outcome = c.orchestrator_commit(attempt.path, attestation=attestation, signer=signer,
                                    subject="spike: N1", policy=POLICY)  # fmt: skip
    stored = entire.read_checkpoint(host, str(outcome.checkpoint_id))
    full = b"".join(s.full for s in stored.sessions)
    assert nonce.encode() in full
    assert token.encode() in full
    assert redaction.honeytoken_hits(host, token)
    assert redaction.honeytoken_hits(host, new_honeytoken()) == []


@needs_model
def test_n5_concurrent_sessions_each_link_their_own_checkpoint(host: Path) -> None:
    attempts = [wt.create(host, "INC-S", f"C{i}") for i in (1, 2, 3)]
    nonces = [new_nonce() for _ in attempts]
    requests = [_request(a.path, _task(n)) for a, n in zip(attempts, nonces, strict=True)]
    with cf.ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(cs.run, requests))
    signer = attest.Ed25519Signer.generate()
    for attempt, nonce, request, result in zip(attempts, nonces, requests, results, strict=True):
        assert result.ok
        attestation = attest.SessionAttestation(
            nonce=nonce, role="surgeon", hypothesis=attempt.hypothesis, incident="INC-S",
            worktree="", base_sha=attempt.base_sha,
            prompt_template_sha=c.prompt_digest(request.prompt), t_start="spike",
        )  # fmt: skip
        outcome = c.orchestrator_commit(
            attempt.path, attestation=attestation, signer=signer,
            subject=f"spike: {attempt.hypothesis}", policy=POLICY,
        )  # fmt: skip
        stored = entire.read_checkpoint(host, str(outcome.checkpoint_id))
        assert {s.session_id for s in stored.sessions} == {request.session_id}
        full = b"".join(s.full for s in stored.sessions)
        assert nonce.encode() in full
        assert not any(other.encode() in full for other in nonces if other != nonce)


@needs_model
def test_a14_kill_mid_tool_use_leaves_no_checkpoint(host: Path) -> None:
    attempt = wt.create(host, "INC-S", "K1")
    request = _request(attempt.path, _task(new_nonce(), extra=" Then write three more files."))
    result = cs.run(request, on_event=lambda e: any(
        b.get("type") == "tool_use" for b in (e.get("message") or {}).get("content") or []
        if isinstance(b, dict)))  # fmt: skip
    assert result.stopped_by_caller
    before = set(entire.local_checkpoint_refs(host))
    entire.session_stop(host, request.session_id)
    wt.reset_clean(attempt)
    assert set(entire.local_checkpoint_refs(host)) == before
    assert result.transcript_path is not None
    assert result.transcript_sha256 == cs.file_sha256(result.transcript_path)


def test_session_ids_are_orchestrator_chosen() -> None:
    request = _request(Path("."), "x", session_id=str(uuid.uuid4()))
    argv = cs.build_argv(request)
    assert argv[argv.index("--session-id") + 1] == request.session_id
    assert proc.resolve_executable(argv[0]).lower().endswith((".exe", "claude"))
