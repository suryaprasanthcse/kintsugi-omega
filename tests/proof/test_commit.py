"""Orchestrator commit protocol and checks C2–C5, with Entire's hook simulated by a real git hook.

The hook appends ``Entire-Checkpoint:`` in its own paragraph (as Entire may), and the checkpoint
ref is written with Entire's exact on-disk layout. The real Entire + Claude Code path is covered
by the contract spikes.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from kintsugi.gates.protected_paths import load_policy
from kintsugi.memory import entire
from kintsugi.proof import attest
from kintsugi.proof import commit as c
from kintsugi.proof.passphrase import new_nonce
from kintsugi.sandbox import proc
from tests.helpers.checkpoints import git, user_line, write_checkpoint

CKPT = "01M4MT4M6K467TEZ3R6J340DAB"
OTHER = "01M4MXCTAM5YSYDX4HXMJ5V7H4"
ARCHITECT = "Surya Prasanth R <suryaprasanthcse@gmail.com>"
POLICY = load_policy()
Signer = attest.Ed25519Signer
PROMPT = "Fix the duplicate journal lines.\nKintsugi-Nonce: {nonce}\n"
HOOK = """#!/bin/sh
# Stand-in for Entire's prepare-commit-msg: appends the trailer in its own paragraph.
printf '\\nEntire-Checkpoint: %s\\n' "{ckpt}" >> "$1"
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    if proc.which("git") is None:
        pytest.skip("git is not installed")
    path = tmp_path / "host"
    proc.run(["git", "init", "-q", "-b", "main", str(path)], timeout_s=30, check=True)
    git(path, "config", "user.name", "Surya Prasanth R")
    git(path, "config", "user.email", "suryaprasanthcse@gmail.com")
    (path / "reconcile.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "base")
    return path


def _install_hook(repo: Path, ckpt: str = CKPT) -> None:
    hook = repo / ".git" / "hooks" / "prepare-commit-msg"
    hook.write_text(HOOK.format(ckpt=ckpt), encoding="utf-8", newline="\n")
    hook.chmod(0o755)


def _session(repo: Path, *, files: tuple[str, ...] = ("reconcile.py",)) -> tuple[str, str]:
    """What a headless session leaves behind: an edit, and a checkpoint holding its transcript."""
    nonce = new_nonce()
    prompt = PROMPT.format(nonce=nonce)
    (repo / "reconcile.py").write_text("def run():\n    return 2\n", encoding="utf-8")
    transcript = user_line(prompt) + b'{"type":"assistant","message":{"content":"done"}}\n'
    write_checkpoint(repo, CKPT, transcript, files_touched=files)
    return nonce, prompt


def _attestation(nonce: str, prompt: str) -> attest.SessionAttestation:
    return attest.SessionAttestation(
        nonce=nonce, role="surgeon", hypothesis="H2", incident="INC-0042",
        worktree="wt/INC-0042/H2", base_sha="0" * 40,
        prompt_template_sha=c.prompt_digest(prompt), t_start="2026-10-12T03:14:07Z",
    )  # fmt: skip


@pytest.fixture
def signer() -> attest.Ed25519Signer:
    return attest.Ed25519Signer.generate()


def _public(signer: Signer) -> Ed25519PublicKey:
    return attest.load_public_key(signer.public_key_openssh())


def test_full_protocol_passes_c2_to_c5(repo: Path, signer: Signer) -> None:
    _install_hook(repo)
    nonce, prompt = _session(repo)
    outcome = c.orchestrator_commit(
        repo, attestation=_attestation(nonce, prompt), signer=signer,
        subject="fix(ledger): hoist ReconcileSession out of the retried closure", policy=POLICY,
    )  # fmt: skip
    assert outcome.checkpoint_id == CKPT
    assert outcome.changed_paths == ("reconcile.py",)
    message = git(repo, "log", "-1", "--format=%B")
    assert "Kintsugi-Nonce: " + nonce in message
    assert git(repo, "log", "-1", "--format=%an <%ae>") == ARCHITECT
    report = c.verify_commit(repo, outcome.sha, public_key=_public(signer))
    assert report.ok, report.problems
    assert report.checkpoint_id == CKPT


def test_c4_fails_when_the_nonce_is_not_in_the_transcript(repo: Path, signer: Signer) -> None:
    _install_hook(repo)
    nonce, prompt = _session(repo)
    c.orchestrator_commit(repo, attestation=_attestation(nonce, prompt), signer=signer,
                          subject="fix: x", policy=POLICY)  # fmt: skip
    stored = entire.read_checkpoint(repo, CKPT)
    assert c.check_c4(stored, nonce, ["reconcile.py"]) == []
    mutated = c.strip_nonce(stored, nonce)
    assert "the attested nonce is not in any session transcript" in c.check_c4(mutated, nonce, [])
    assert c.check_c4(stored, new_nonce(), []) != []  # a different nonce is not there either


def test_c4_fails_for_files_the_checkpoint_does_not_record(repo: Path) -> None:
    write_checkpoint(repo, CKPT, user_line("x"), files_touched=("other.py",))
    stored = entire.read_checkpoint(repo, CKPT)
    assert any("does not record" in p for p in c.check_c4(stored, "x", ["reconcile.py"]))


def test_c4_fails_for_a_tampered_transcript(repo: Path) -> None:
    write_checkpoint(repo, CKPT, user_line("nonce-here"), tamper=True)
    stored = entire.read_checkpoint(repo, CKPT)
    assert any("content_hash" in p for p in c.check_c4(stored, "nonce-here", []))


def test_c5_detects_an_altered_prompt(repo: Path) -> None:
    write_checkpoint(repo, CKPT, user_line("the real prompt"))
    stored = entire.read_checkpoint(repo, CKPT)
    assert c.check_c5(stored, c.prompt_digest("the real prompt")) == []
    assert c.check_c5(stored, c.prompt_digest("the real prompt ")) != []


def test_c3_detects_a_forged_signature(repo: Path, signer: Signer) -> None:
    _install_hook(repo)
    nonce, prompt = _session(repo)
    outcome = c.orchestrator_commit(repo, attestation=_attestation(nonce, prompt), signer=signer,
                                    subject="fix: x", policy=POLICY)  # fmt: skip
    other = attest.Ed25519Signer.generate()
    report = c.verify_commit(repo, outcome.sha, public_key=_public(other))
    assert report.problems["C3"] == ["bad signature"]


def test_no_checkpoint_means_the_commit_is_undone(repo: Path, signer: Signer) -> None:
    nonce, prompt = _session(repo)  # no hook installed: nothing links the commit
    base = git(repo, "rev-parse", "HEAD")
    with pytest.raises(c.CheckpointMissing, match="Entire-Checkpoint"):
        c.orchestrator_commit(repo, attestation=_attestation(nonce, prompt), signer=signer,
                              subject="fix: x", policy=POLICY)  # fmt: skip
    assert git(repo, "rev-parse", "HEAD") == base  # the unlinked commit is gone …
    assert "reconcile.py" in git(repo, "diff", "--cached", "--name-only")  # … the change kept


def test_a_trailer_naming_a_missing_ref_is_also_refused(repo: Path, signer: Signer) -> None:
    _install_hook(repo, ckpt="01M4MXCTAM5YSYDX4HXMJ5V7H4")  # no such ref
    nonce, prompt = _session(repo)
    with pytest.raises(c.CheckpointMissing, match="does not exist"):
        c.orchestrator_commit(repo, attestation=_attestation(nonce, prompt), signer=signer,
                              subject="fix: x", policy=POLICY)  # fmt: skip


def test_protected_paths_are_refused_before_committing(repo: Path, signer: Signer) -> None:
    nonce, prompt = _session(repo)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_reconcile.py").write_text("assert True\n", encoding="utf-8")
    with pytest.raises(c.CommitRefused, match="protected paths"):
        c.orchestrator_commit(repo, attestation=_attestation(nonce, prompt), signer=signer,
                              subject="fix: x", policy=POLICY)  # fmt: skip
    assert git(repo, "diff", "--cached", "--name-only") == ""  # index reset


def test_diff_budget(repo: Path, signer: Signer) -> None:
    _install_hook(repo)
    nonce, prompt = _session(repo)
    (repo / "big.py").write_text("x = 1\n" * 200, encoding="utf-8")
    with pytest.raises(c.CommitRefused, match="budget"):
        c.orchestrator_commit(repo, attestation=_attestation(nonce, prompt), signer=signer,
                              subject="fix: x", policy=POLICY)  # fmt: skip
    write_checkpoint(repo, CKPT, user_line(prompt), files_touched=("reconcile.py", "big.py"))
    outcome = c.orchestrator_commit(
        repo, attestation=_attestation(nonce, prompt), signer=signer, subject="fix: x",
        policy=POLICY, approved_large_diff=True,
    )  # fmt: skip
    assert outcome.changed_lines > POLICY.diff_budget_lines


def test_changes_outside_the_sessions_tool_calls_are_refused(repo: Path, signer: Signer) -> None:
    # Found on a real checkpoint: a file written by a shell command is not in files_touched.
    _install_hook(repo)
    nonce, prompt = _session(repo)
    (repo / "formatter_output.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(c.CommitRefused, match="do not explain"):
        c.orchestrator_commit(
            repo, attestation=_attestation(nonce, prompt), signer=signer, subject="fix: x",
            policy=POLICY, session_paths={"reconcile.py"},
        )  # fmt: skip
    assert git(repo, "diff", "--cached", "--name-only") == ""


def test_tool_edited_paths_are_worktree_relative(tmp_path: Path) -> None:
    uses = [
        {"name": "Write", "input": {"file_path": str(tmp_path / "ledger" / "reconcile.py")}},
        {"name": "Edit", "input": {"file_path": "README.md"}},
        {"name": "Read", "input": {"file_path": str(tmp_path / "secret.txt")}},
        {"name": "Write", "input": {"file_path": str(tmp_path.parent / "outside.py")}},
        {"name": "Bash", "input": {"command": "pytest -q"}},
    ]
    assert c.tool_edited_paths(uses, tmp_path) == {"ledger/reconcile.py", "README.md"}


def test_an_empty_session_is_refused(repo: Path, signer: Signer) -> None:
    attestation = _attestation(new_nonce(), "p")
    with pytest.raises(c.CommitRefused, match="changed nothing"):
        c.orchestrator_commit(
            repo, attestation=attestation, signer=signer, subject="x", policy=POLICY
        )


@pytest.mark.parametrize(
    "line", ["Co-authored-by: Claude <noreply@anthropic.com>", "Claude-Session: https://x"]
)
def test_forbidden_trailers(line: str) -> None:
    problems = c.message_problems(f"fix: x\n\n{line}\n", require_attestation=False,
                                  require_checkpoint=False)  # fmt: skip
    assert len(problems) == 1
    assert "forbidden trailer" in problems[0]


def test_a_forbidden_trailer_in_the_body_blocks_the_commit(repo: Path, signer: Signer) -> None:
    _install_hook(repo)
    nonce, prompt = _session(repo)
    with pytest.raises(c.CommitRefused, match="forbidden"):
        c.orchestrator_commit(repo, attestation=_attestation(nonce, prompt), signer=signer,
                              subject="fix: x", body="Co-authored-by: Claude <x@y>",
                              policy=POLICY)  # fmt: skip


def test_checkpoint_trailer_must_be_a_single_ulid() -> None:
    message = "fix\n\nEntire-Checkpoint: ck_7f3a\n"
    assert c.message_problems(message, require_attestation=False, require_checkpoint=True)


def test_first_user_prompt_handles_both_content_shapes() -> None:
    blocks = (
        b'{"type":"user","isMeta":true,"message":{"content":"meta"}}\n'
        b'{"type":"user","message":{"content":[{"type":"text","text":"real "},'
        b'{"type":"text","text":"prompt"}]}}\n'
    )
    assert c.first_user_prompt(blocks) == "real prompt"
    assert c.first_user_prompt(user_line("plain")) == "plain"
    assert c.first_user_prompt(b'{"type":"assistant"}\nnot json\n') is None


def test_verify_reports_a_missing_checkpoint(repo: Path, signer: Signer) -> None:
    nonce, prompt = _session(repo)
    git(repo, "add", "-A")
    tree, parent = git(repo, "write-tree"), git(repo, "rev-parse", "HEAD")
    signed = attest.attest(_attestation(nonce, prompt), signer, tree_sha=tree, parent_sha=parent)
    trailers = attest.format_trailers(attest.trailers(signed))
    git(repo, "commit", "-q", "-m", f"fix: x\n\n{trailers}\nEntire-Checkpoint: {OTHER}")
    report = c.verify_commit(repo, git(repo, "rev-parse", "HEAD"), public_key=_public(signer))
    [problem] = report.problems["C4"]
    assert "has no ref" in problem
    assert not report.ok


def test_signed_attestation_copy_is_not_mutated(repo: Path, signer: Signer) -> None:
    original = _attestation(new_nonce(), "p")
    signed = attest.attest(original, signer, tree_sha="a" * 40, parent_sha="b" * 40)
    assert original.signature is None
    assert replace(signed, signature=None).tree_sha == "a" * 40
