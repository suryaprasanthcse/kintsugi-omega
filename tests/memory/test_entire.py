"""Entire adapter: strict parsing of real CLI output, checkpoint refs via plain git, ref sync."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kintsugi.memory import entire
from kintsugi.sandbox import proc
from tests.helpers.checkpoints import git, write_checkpoint

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "entire"
CKPT = "01M4MT4M6K467TEZ3R6J340DAB"


def _fake_cli(monkeypatch: pytest.MonkeyPatch, outputs: dict[str, str]) -> None:
    def run(args: list[str], repo: object, *, timeout_s: float = 120.0) -> str:
        key = " ".join(args[:2])
        return outputs[key]

    monkeypatch.setattr(entire, "_run", run)


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_checkpoint_list_parses_real_output(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_cli(monkeypatch, {"checkpoint list": _fixture("checkpoint_list.json")})
    [first, *_] = entire.checkpoint_list(".")
    assert first.agent == "Claude Code"
    assert first.session_id in first.session_ids


def test_checkpoint_explain_parses_real_output(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_cli(monkeypatch, {"checkpoint explain": _fixture("checkpoint_explain.json")})
    explained = entire.checkpoint_explain(".", CKPT)
    assert explained.checkpoint_id == CKPT
    assert explained.strategy == "manual-commit"
    assert "README.md" in explained.files_touched
    assert explained.sessions[0].model == "claude-opus-5-5"


def test_why_parses_real_output(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_cli(monkeypatch, {"why CLAUDE.md:20": _fixture("why.json")})
    line = entire.why(".", "CLAUDE.md", 20)
    assert line.authorship == "ai"
    assert line.checkpoint_id == CKPT
    assert line.commit_sha.startswith("c299a26")


def test_session_list_parses_real_output(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_cli(monkeypatch, {"session list": _fixture("session_list.json")})
    [session] = entire.session_list(".")
    assert session.agent == "Claude Code"
    assert session.status == "active"


def test_missing_required_fields_fail_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    broken = json.loads(_fixture("checkpoint_explain.json"))
    del broken["sessions"]
    _fake_cli(monkeypatch, {"checkpoint explain": json.dumps(broken)})
    with pytest.raises(entire.EntireSchemaError, match="sessions"):
        entire.checkpoint_explain(".", CKPT)


def test_non_json_output_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_cli(monkeypatch, {"checkpoint list": "Not JSON at all"})
    with pytest.raises(entire.EntireSchemaError, match="expected JSON"):
        entire.checkpoint_list(".")


def test_version_text_is_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_cli(monkeypatch, {"version": "Entire CLI 0.11.4\nGo version: go1.27.1\n"})
    assert entire.version() == "0.11.4"
    entire.require_pinned()
    _fake_cli(monkeypatch, {"version": "Entire CLI 0.12.0\n"})
    with pytest.raises(entire.EntireError, match="not the pinned"):
        entire.require_pinned()


def test_checkpoint_ref_shards_on_the_last_two_characters() -> None:
    assert entire.checkpoint_ref(CKPT) == f"refs/entire/checkpoints/AB/{CKPT}"
    with pytest.raises(ValueError, match="not a checkpoint id"):
        entire.checkpoint_ref("ck_7f3a")


# ---- checkpoint refs with plain git (CI-safe) ------------------------------------------------

_git = git
_write_checkpoint = write_checkpoint


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    if proc.which("git") is None:
        pytest.skip("git is not installed")
    path = tmp_path / "repo"
    proc.run(["git", "init", "-q", str(path)], timeout_s=30, check=True)
    return path


def test_read_checkpoint_from_its_ref(repo: Path) -> None:
    full = b'{"type":"user","message":{"content":"Kintsugi-Nonce: bafo-kimu"}}\n'
    _write_checkpoint(repo, CKPT, full)
    stored = entire.read_checkpoint(repo, CKPT)
    assert stored.ref == f"refs/entire/checkpoints/AB/{CKPT}"
    assert stored.files_touched == ["attempt.txt"]
    [session] = stored.sessions
    assert session.full == full
    assert session.session_id == "sess-1"
    assert session.intact


def test_a_tampered_transcript_no_longer_matches_its_content_hash(repo: Path) -> None:
    _write_checkpoint(repo, CKPT, b'{"type":"user"}\n', tamper=True)
    assert not entire.read_checkpoint(repo, CKPT).sessions[0].intact


def test_reading_a_missing_checkpoint_fails(repo: Path) -> None:
    with pytest.raises(entire.EntireError, match="has no ref"):
        entire.read_checkpoint(repo, CKPT)


def test_sync_pushes_only_stale_refs_and_verifies_the_remote(tmp_path: Path, repo: Path) -> None:
    remote = tmp_path / "remote.git"
    proc.run(["git", "init", "-q", "--bare", str(remote)], timeout_s=30, check=True)
    _git(repo, "remote", "add", "origin", str(remote))
    other = "01M4MXCTAM5YSYDX4HXMJ5V7H4"
    _write_checkpoint(repo, CKPT, b"a\n")
    _write_checkpoint(repo, other, b"b\n")
    first = entire.sync_checkpoint_refs(repo)
    assert first.in_sync
    assert len(first.pushed) == 2
    _write_checkpoint(repo, other, b"finalized transcript\n", finalize=True)  # a Stop hook
    second = entire.sync_checkpoint_refs(repo)
    assert second.pushed == (entire.checkpoint_ref(other),)
    assert second.in_sync
    assert entire.remote_checkpoint_refs(repo) == entire.local_checkpoint_refs(repo)


def test_sync_refuses_to_push_a_transcript_with_a_secret(tmp_path: Path, repo: Path) -> None:
    from kintsugi.proof.redaction import SecretsFound

    remote = tmp_path / "remote.git"
    proc.run(["git", "init", "-q", "--bare", str(remote)], timeout_s=30, check=True)
    _git(repo, "remote", "add", "origin", str(remote))
    leaked = "sk-" + "ant-api03-" + "Z" * 40  # assembled at runtime: no secret literal in the repo
    _write_checkpoint(repo, CKPT, f'{{"type":"user","content":"{leaked}"}}\n'.encode())
    with pytest.raises(SecretsFound):
        entire.sync_checkpoint_refs(repo)
    assert entire.remote_checkpoint_refs(repo) == {}  # nothing left the machine


def test_sync_never_forces_a_diverged_ref(tmp_path: Path, repo: Path) -> None:
    # A non-fast-forward means divergence. Forcing could destroy evidence, so it is reported.
    remote = tmp_path / "remote.git"
    proc.run(["git", "init", "-q", "--bare", str(remote)], timeout_s=30, check=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _write_checkpoint(repo, CKPT, b"original\n")
    assert entire.sync_checkpoint_refs(repo).in_sync
    _write_checkpoint(repo, CKPT, b"rewritten history\n")  # a new root: not a fast-forward
    report = entire.sync_checkpoint_refs(repo)
    assert report.out_of_sync == (entire.checkpoint_ref(CKPT),)
