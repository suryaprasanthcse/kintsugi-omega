"""Headless session runner: argv, real stream-json parsing, stdin prompt, stop and timeouts.

The run loop is exercised against a stand-in Claude Code (a Python script that speaks the same
stream-json protocol); the real binary is exercised by the contract spikes.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from kintsugi.crew import claude_session as cs

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FIXTURE = FIXTURES / "claude" / "stream_auth_failure.jsonl"

FAKE_CLAUDE = r"""
import json, pathlib, sys, time
args = sys.argv[1:]
sid = args[args.index("--session-id") + 1]
home = pathlib.Path(sys.argv[0]).parent / "home"
prompt = sys.stdin.buffer.read()
def emit(event):
    print(json.dumps(event), flush=True)
init = {"type": "system", "subtype": "init", "session_id": sid, "model": "fake"}
emit({**init, "apiKeySource": "none"})
transcript = home / "projects" / "C--fake" / f"{sid}.jsonl"
transcript.parent.mkdir(parents=True, exist_ok=True)
entry = {"type": "user", "message": {"content": prompt.decode()}}
transcript.write_bytes(json.dumps(entry).encode() + b"\n")
emit({"type": "assistant", "session_id": sid, "message": {"content": [
    {"type": "tool_use", "name": "Write", "input": {"file_path": "attempt.txt", "content": "x"}}]}})
if b"SLOW" in prompt:
    time.sleep(120)
emit({"type": "result", "subtype": "success", "session_id": sid, "is_error": False,
      "total_cost_usd": 0.0123, "num_turns": 2, "usage": {"input_tokens": 10, "output_tokens": 5},
      "prompt_bytes": len(prompt)})
"""


def test_real_auth_failure_stream_is_understood() -> None:
    lines = FIXTURE.read_text(encoding="utf-8").splitlines()
    summary = cs.parse_stream(lines)
    assert summary.session_id == "dacd63ef-15b9-4e3d-ae47-08c7416577cc"
    assert summary.api_key_source == "none"
    assert summary.error == cs.AUTH_FAILURE
    assert summary.is_error
    assert summary.cost_usd == 0.0
    assert summary.num_turns == 1


def test_parser_tolerates_noise() -> None:
    summary = cs.parse_stream(["", "not json", "[1, 2]", '{"type": "result", "num_turns": 3}'])
    assert summary.unparsed_lines == 1
    assert summary.num_turns == 3


def test_argv_keeps_the_prompt_off_the_command_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KINTSUGI_CLAUDE_BIN", raising=False)
    request = cs.SessionRequest(
        prompt="Kintsugi-Nonce: secret-ish & | > text", cwd=Path("."),
        allowed_tools=("Read", "Edit"), max_budget_usd=1.5, session_id="sid-1",
    )  # fmt: skip
    argv = cs.build_argv(request)
    assert argv[0] == "claude"
    assert request.prompt not in " ".join(argv)
    assert argv[argv.index("--allowedTools") + 1] == "Read,Edit"
    assert argv[argv.index("--session-id") + 1] == "sid-1"
    assert argv[argv.index("--max-budget-usd") + 1] == "1.50"
    assert argv[argv.index("--setting-sources") + 1] == "project,local"
    assert "--strict-mcp-config" in argv
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings["attribution"] == {"commit": "", "pr": "", "sessionUrl": False}


def test_a_resumed_session_continues_its_own_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KINTSUGI_CLAUDE_BIN", raising=False)
    request = cs.SessionRequest(prompt="go on", cwd=Path("."), allowed_tools=("Read",),
                                session_id="sid-2", resume=True)  # fmt: skip
    argv = cs.build_argv(request)
    assert argv[argv.index("--resume") + 1] == "sid-2"
    assert "--session-id" not in argv  # spike N7: resume the session, never fork a new id


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE, encoding="utf-8")
    original = cs.build_argv

    def argv(request: cs.SessionRequest) -> list[str]:
        return [sys.executable, str(script), *original(request)[1:]]

    monkeypatch.setattr(cs, "build_argv", argv)
    return tmp_path / "home"


def _request(tmp_path: Path, prompt: str, **kw: Any) -> cs.SessionRequest:
    return cs.SessionRequest(prompt=prompt, cwd=tmp_path, allowed_tools=("Write",), **kw)


def test_run_delivers_the_prompt_and_meters_the_session(tmp_path: Path, fake_claude: Path) -> None:
    prompt = 'Kintsugi-Nonce: bafo-kimu\nline two & | > "quotes" Ω\n' + "x" * 50_000
    result = cs.run(_request(tmp_path, prompt), claude_home=fake_claude)
    assert result.ok
    assert result.stream.result is not None
    assert result.stream.result["prompt_bytes"] == len(prompt.encode())  # exact, beyond argv limits
    assert result.stream.cost_usd == pytest.approx(0.0123)
    assert result.stream.tool_uses == [
        {"name": "Write", "input": {"file_path": "attempt.txt", "content": "x"}}
    ]
    assert result.transcript_path is not None
    assert result.transcript_path.name == f"{result.session_id}.jsonl"
    assert result.transcript_sha256 == cs.file_sha256(result.transcript_path)


def test_on_event_can_stop_a_session_mid_flight(tmp_path: Path, fake_claude: Path) -> None:
    started = time.monotonic()
    result = cs.run(
        _request(tmp_path, "SLOW"),
        on_event=lambda event: event.get("type") == "assistant",
        claude_home=fake_claude,
    )
    assert result.stopped_by_caller
    assert not result.ok
    assert time.monotonic() - started < 60
    assert result.transcript_path is not None  # the evidence of the abandoned attempt (A14)


def test_timeout_kills_the_session(tmp_path: Path, fake_claude: Path) -> None:
    result = cs.run(_request(tmp_path, "SLOW", timeout_s=5), claude_home=fake_claude)
    assert result.timed_out
    assert not result.ok


def test_find_transcript_is_none_when_absent(tmp_path: Path) -> None:
    assert cs.find_transcript("no-such-session", tmp_path) is None


def test_an_api_key_satisfies_the_login_check() -> None:
    cs.require_logged_in({"ANTHROPIC_API_KEY": "sk-ant-test-not-real"})
