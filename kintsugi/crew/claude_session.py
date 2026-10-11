"""Headless Claude Code sessions (spec §5.7 Surgeon, §4.5.2; plan §2a, §2d, ruling D7).

A session is ``claude -p --output-format stream-json --verbose`` launched through the subprocess
gateway, so it always runs with ``ENTIRE_TEST_TTY=0`` and an allowlisted environment. Choices:

- The prompt goes in on **stdin from a file**: exact bytes (audit C5 hashes them), no command
  line length limit (Windows caps it near 32k characters), no shell quoting.
- ``--session-id`` is chosen by the orchestrator, so an attempt's session is known before it
  starts (L2 adoption) and Entire records it under that ID.
- ``--settings`` hides commit attribution (ruling D4); ``--strict-mcp-config`` and
  ``--setting-sources project,local`` keep the user's MCP servers, plugins, skills and hooks out
  (the repository's own hooks, Entire's included, still apply); ``--max-budget-usd`` is a
  per-session hard cap (ruling D7).
- Cost, usage and turns come from the stream's ``result`` event; the native JSONL transcript is
  located under ``~/.claude/projects`` and hashed, which is the evidence for an attempt that was
  killed before it could commit (L2, spike A14).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kintsugi.sandbox import proc

DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_SETTINGS: Mapping[str, Any] = {"attribution": {"commit": "", "pr": "", "sessionUrl": False}}
AUTH_FAILURE = "authentication_failed"


class SessionError(RuntimeError):
    pass


class NotLoggedIn(SessionError):
    """The headless CLI has no credentials (``claude auth login`` or ``ANTHROPIC_API_KEY``)."""


def executable() -> str:
    """The Claude Code binary: ``KINTSUGI_CLAUDE_BIN`` if set, else ``claude`` on PATH."""
    return os.environ.get("KINTSUGI_CLAUDE_BIN") or "claude"


@dataclass(frozen=True)
class SessionRequest:
    prompt: str
    cwd: Path
    allowed_tools: tuple[str, ...]
    model: str = DEFAULT_MODEL
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    max_budget_usd: float | None = None
    timeout_s: float = 2700.0  # the Surgeon activity's start_to_close (spec §5.3.2)
    settings: Mapping[str, Any] = field(default_factory=lambda: dict(DEFAULT_SETTINGS))
    extra_env: Mapping[str, str] = field(default_factory=dict)
    append_system_prompt: str | None = None
    resume: bool = False  # continue ``session_id`` (spike N7) instead of starting it
    # Crew roles other than the Surgeon (spec §5.7) answer one structured question: no tools,
    # their own system prompt, a JSON schema, no settings or session files of their own.
    tools: tuple[str, ...] | None = None  # ``--tools``: None keeps the defaults, () disables all
    system_prompt: str | None = None
    json_schema: Mapping[str, Any] | None = None
    setting_sources: str = "project,local"  # no user plugins, skills or hooks in a Surgeon
    persist: bool = True  # False: ``--no-session-persistence`` (nothing under ~/.claude)


def build_argv(request: SessionRequest) -> list[str]:
    """The command line. The prompt itself is not on it: it is fed on stdin."""
    argv = [
        executable(), "-p",
        "--output-format", "stream-json", "--verbose",
        "--model", request.model,
        "--resume" if request.resume else "--session-id", request.session_id,
    ]  # fmt: skip
    if request.allowed_tools:
        argv += ["--allowedTools", ",".join(request.allowed_tools)]
    if request.tools is not None:
        argv += ["--tools", ",".join(request.tools)]
    argv += [
        "--strict-mcp-config",
        "--setting-sources", request.setting_sources,
        "--settings", json.dumps(dict(request.settings), separators=(",", ":")),
    ]  # fmt: skip
    if request.max_budget_usd is not None:
        argv += ["--max-budget-usd", f"{request.max_budget_usd:.2f}"]
    if request.system_prompt is not None:
        argv += ["--system-prompt", request.system_prompt]
    if request.append_system_prompt:
        argv += ["--append-system-prompt", request.append_system_prompt]
    if request.json_schema is not None:
        argv += ["--json-schema", json.dumps(dict(request.json_schema), separators=(",", ":"))]
    if not request.persist:
        argv += ["--no-session-persistence"]
    return argv


@dataclass
class StreamSummary:
    session_id: str | None = None
    model: str | None = None
    api_key_source: str | None = None
    tool_uses: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None
    unparsed_lines: int = 0
    events: int = 0

    @property
    def cost_usd(self) -> float:
        return float((self.result or {}).get("total_cost_usd") or 0.0)

    @property
    def usage(self) -> dict[str, Any]:
        return dict((self.result or {}).get("usage") or {})

    @property
    def num_turns(self) -> int:
        return int((self.result or {}).get("num_turns") or 0)

    @property
    def is_error(self) -> bool:
        return self.error is not None or bool((self.result or {}).get("is_error"))

    def feed(self, event: Mapping[str, Any]) -> None:
        self.events += 1
        kind, subtype = event.get("type"), event.get("subtype")
        if kind == "system" and subtype == "init":
            self.session_id = event.get("session_id", self.session_id)
            self.model = event.get("model")
            self.api_key_source = event.get("apiKeySource")
        elif kind == "assistant":
            if event.get("error"):
                self.error = str(event["error"])
            content = (event.get("message") or {}).get("content") or []
            self.tool_uses += [
                {"name": block.get("name"), "input": block.get("input", {})}
                for block in content
                if isinstance(block, dict) and block.get("type") == "tool_use"
            ]
        elif kind == "result":
            self.result = dict(event)
            self.session_id = event.get("session_id", self.session_id)


def parse_stream(lines: Iterable[str]) -> StreamSummary:
    summary = StreamSummary()
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            summary.unparsed_lines += 1
            continue
        if isinstance(event, dict):
            summary.feed(event)
    return summary


@dataclass(frozen=True)
class SessionResult:
    request: SessionRequest
    stream: StreamSummary
    exit_code: int | None
    timed_out: bool
    stopped_by_caller: bool
    transcript_path: Path | None
    transcript_sha256: str | None

    @property
    def session_id(self) -> str:
        return self.stream.session_id or self.request.session_id

    @property
    def ok(self) -> bool:
        return (
            self.exit_code == 0
            and not self.timed_out
            and not self.stopped_by_caller
            and not self.stream.is_error
        )


def find_transcript(session_id: str, claude_home: Path | None = None) -> Path | None:
    """Claude Code's native transcript: ``~/.claude/projects/<sanitised cwd>/<session>.jsonl``."""
    home = claude_home or Path.home() / ".claude"
    matches = sorted((home / "projects").glob(f"*/{session_id}.jsonl"))
    return matches[0] if matches else None


def file_sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def run(
    request: SessionRequest,
    *,
    on_event: Callable[[Mapping[str, Any]], bool] | None = None,
    claude_home: Path | None = None,
    on_start: Callable[[int], None] | None = None,
) -> SessionResult:
    """Run one session to completion. ``on_event`` returning True stops it (tree kill).

    ``on_start(pid)`` runs as soon as the child exists: the Surgeon step records the pid in the
    worktree lock, so a restarted worker can kill an orphaned session before it adopts or resets
    the worktree (spec §6.6)."""
    stream = StreamSummary()
    timed_out = threading.Event()
    stopped = False
    with tempfile.TemporaryDirectory(prefix="kintsugi-session-") as scratch:
        prompt_file = Path(scratch) / "prompt.txt"
        prompt_file.write_bytes(request.prompt.encode("utf-8"))
        child = proc.start(
            build_argv(request),
            cwd=request.cwd,
            extra_env=request.extra_env,
            stderr_to=Path(scratch) / "stderr.log",
            stdin_path=prompt_file,
        )
        if on_start is not None:
            on_start(child.pid)
        timer = threading.Timer(request.timeout_s, lambda: (timed_out.set(), child.kill_tree()))
        timer.start()
        try:
            for line in child.lines():
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    stream.unparsed_lines += 1
                    continue
                stream.feed(event)
                if on_event is not None and on_event(event):
                    stopped = True
                    child.kill_tree()
                    break
            exit_code = child.wait(timeout_s=60)
        finally:
            timer.cancel()
            child.kill_tree()
    session_id = stream.session_id or request.session_id
    transcript = find_transcript(session_id, claude_home)
    return SessionResult(
        request=request,
        stream=stream,
        exit_code=exit_code,
        timed_out=timed_out.is_set(),
        stopped_by_caller=stopped,
        transcript_path=transcript,
        transcript_sha256=file_sha256(transcript) if transcript else None,
    )


def auth_status() -> dict[str, Any]:
    result = proc.run([executable(), "auth", "status"], timeout_s=60)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"loggedIn": False, "raw": result.stdout[-500:]}


def require_logged_in(extra_env: Mapping[str, str] | None = None) -> None:
    if extra_env and extra_env.get("ANTHROPIC_API_KEY"):
        return
    if not auth_status().get("loggedIn"):
        raise NotLoggedIn("the headless Claude Code CLI is not logged in: run `claude auth login`")
