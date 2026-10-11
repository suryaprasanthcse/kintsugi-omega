"""Entire adapter: strict parsing of real CLI output, checkpoint refs via plain git, ref sync."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from kintsugi.memory import entire
from kintsugi.sandbox import proc

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


def _git(repo: Path, *args: str, input_text: str | None = None) -> str:
    result = proc.run(["git", "-C", str(repo), *args], timeout_s=60, input_text=input_text)
    assert result.ok, result.stderr
    return result.stdout.strip()


def _blob(repo: Path, content: bytes) -> str:
    path = repo / ".blob"
    path.write_bytes(content)
    sha = _git(repo, "hash-object", "-w", str(path))
    path.unlink()
    return sha


def _write_checkpoint(
    repo: Path, checkpoint_id: str, full: bytes, *, tamper: bool = False, finalize: bool = False
) -> str:
    """Build a ref with Entire's exact on-disk layout (verified on real checkpoints)."""
    session_meta = {"checkpoint_id": checkpoint_id, "session_id": "sess-1", "turn_id": "t1"}
    top = {
        "checkpoint_id": checkpoint_id,
        "strategy": "manual-commit",
        "files_touched": ["attempt.txt"],
        "sessions": [{"metadata": "/0/metadata.json", "transcript": "/0/full.jsonl",
                      "compact_transcript": "/0/transcript.jsonl",
                      "content_hash": "/0/content_hash.txt", "prompt": ""}],
    }  # fmt: skip
    recorded = hashlib.sha256(full).hexdigest()
    stored_full = full + b'{"tampered": true}\n' if tamper else full
    inner = "\n".join(
        f"100644 blob {sha}\t{name}"
        for name, sha in sorted(
            {
                "content_hash.txt": _blob(repo, f"sha256:{recorded}".encode()),
                "full.jsonl": _blob(repo, stored_full),
                "metadata.json": _blob(repo, json.dumps(session_meta).encode()),
                "transcript.jsonl": _blob(repo, b'{"type":"user","content":"x"}\n'),
            }.items()
        )
    )
    subtree = _git(repo, "mktree", input_text=inner + "\n")
    top_blob = _blob(repo, json.dumps(top).encode())
    listing = f"040000 tree {subtree}\t0\n100644 blob {top_blob}\tmetadata.json\n"
    root = _git(repo, "mktree", input_text=listing)
    ref = entire.checkpoint_ref(checkpoint_id)
    # Entire's "Finalize transcript" commit is a child of the checkpoint commit (fast-forward).
    parent = ["-p", _git(repo, "rev-parse", ref)] if finalize else []
    subject = "Finalize transcript for Checkpoint" if finalize else "Checkpoint"
    commit = _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                  "commit-tree", root, *parent, "-m", f"{subject}: {checkpoint_id}")  # fmt: skip
    _git(repo, "update-ref", ref, commit)
    return commit


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
