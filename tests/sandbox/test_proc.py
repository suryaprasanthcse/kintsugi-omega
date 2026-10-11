from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from kintsugi.sandbox import proc

PY = sys.executable


def _child_env(keys: list[str], extra_env: dict[str, str] | None = None) -> dict[str, str | None]:
    script = f"import json, os; print(json.dumps({{k: os.environ.get(k) for k in {keys!r}}}))"
    result = proc.run([PY, "-c", script], timeout_s=30, check=True, extra_env=extra_env)
    return json.loads(result.stdout)


def test_forced_variables_reach_every_child(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENTIRE_TEST_TTY", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")
    monkeypatch.setenv("GIT_EDITOR", "vim")
    seen = _child_env(["ENTIRE_TEST_TTY", "GIT_TERMINAL_PROMPT", "GIT_EDITOR"])
    assert seen == {"ENTIRE_TEST_TTY": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true"}


@pytest.mark.parametrize("name", ["ENTIRE_TEST_TTY", "entire_test_tty", "GIT_EDITOR"])
def test_forced_variables_cannot_be_overridden(name: str) -> None:
    with pytest.raises(ValueError, match="forced"):
        proc.build_env({name: "1"})


def test_secrets_are_not_inherited(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    monkeypatch.setenv("NEATLOGS_API_KEY", "nl-test-not-a-real-key")
    seen = _child_env(["ANTHROPIC_API_KEY", "NEATLOGS_API_KEY"])
    assert seen == {"ANTHROPIC_API_KEY": None, "NEATLOGS_API_KEY": None}


def test_secrets_pass_only_when_given_explicitly() -> None:
    seen = _child_env(["NEATLOGS_API_KEY"], extra_env={"NEATLOGS_API_KEY": "explicit"})
    assert seen == {"NEATLOGS_API_KEY": "explicit"}


def test_allowlist_is_case_insensitive() -> None:
    env = proc.build_env(inherit={"Path": "/bin", "SECRET_TOKEN": "x"})
    assert env["Path"] == "/bin"
    assert "SECRET_TOKEN" not in env


def test_stdin_is_closed_so_nothing_waits_on_a_terminal() -> None:
    result = proc.run([PY, "-c", "import sys; print(repr(sys.stdin.read()))"], timeout_s=30)
    assert result.ok
    assert result.stdout.strip() == "''"


def test_input_text_is_delivered() -> None:
    result = proc.run(
        [PY, "-c", "import sys; print(sys.stdin.read().upper())"], input_text="seal", timeout_s=30
    )
    assert result.stdout.strip() == "SEAL"


def test_stdin_bytes_are_exact_on_every_platform() -> None:
    # Text-mode pipes on Windows rewrite "\n" as "\r\n": that would corrupt commit messages.
    script = "import sys; print(repr(sys.stdin.buffer.read()))"
    result = proc.run([PY, "-c", script], input_text="a\nb\nΩ", timeout_s=30, check=True)
    assert result.stdout.strip() == repr("a\nb\nΩ".encode())


def test_batch_shims_resolve_to_their_executable(tmp_path: Path) -> None:
    exe = tmp_path / "node_modules" / "tool" / "bin" / "tool.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    shim = tmp_path / "tool.cmd"
    shim.write_text(
        '@ECHO off\n"%dp0%\\node_modules\\tool\\bin\\tool.exe"   %*\n', encoding="utf-8"
    )
    assert proc.exe_from_batch_shim(shim) == str(exe)
    assert proc.resolve_executable(str(shim)) == str(exe)


def test_unresolvable_batch_files_are_refused(tmp_path: Path) -> None:
    script = tmp_path / "run.bat"
    script.write_text("@echo %*\n", encoding="utf-8")
    with pytest.raises(proc.UnsafeExecutable, match="batch file"):
        proc.resolve_executable(str(script))


def test_missing_executables_are_reported() -> None:
    with pytest.raises(FileNotFoundError, match="not found"):
        proc.resolve_executable("kintsugi-no-such-tool-xyz")


def test_run_binary_returns_exact_bytes() -> None:
    script = "import sys; sys.stdout.buffer.write(b'line1\\r\\nline2\\n\\xff')"
    assert proc.run_binary([PY, "-c", script], timeout_s=30) == b"line1\r\nline2\n\xff"
    with pytest.raises(proc.ProcError):
        proc.run_binary([PY, "-c", "import sys; sys.exit(4)"], timeout_s=30)


def test_utf8_round_trip() -> None:
    result = proc.run([PY, "-c", "print('KINTSUGI-Ω ‖ ×')"], timeout_s=30, check=True)
    assert result.stdout.strip() == "KINTSUGI-Ω ‖ ×"


def test_shell_strings_are_rejected() -> None:
    with pytest.raises(TypeError):
        proc.run("echo hi")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="empty"):
        proc.run([])


def test_check_raises_on_failure() -> None:
    with pytest.raises(proc.ProcError) as info:
        proc.run([PY, "-c", "import sys; sys.exit(3)"], timeout_s=30, check=True)
    assert info.value.result.returncode == 3


def _wait_until_dead(pid: int, within_s: float = 10.0) -> bool:
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        if not proc.pid_alive(pid):
            return True
        time.sleep(0.1)
    return False


def test_timeout_kills_the_whole_tree(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    # The child starts a grandchild that would outlive it, then both sleep far past the timeout.
    child = (
        "import pathlib, subprocess, sys, time\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(g.pid))\n"
        "time.sleep(120)\n"
    )
    started = time.monotonic()
    result = proc.run([PY, "-c", child], timeout_s=5)
    assert result.timed_out
    assert not result.ok
    assert time.monotonic() - started < 30
    grandchild = int(pid_file.read_text())
    assert _wait_until_dead(grandchild), "grandchild survived the tree kill"


def test_timeout_with_check_raises_proc_timeout() -> None:
    with pytest.raises(proc.ProcTimeout):
        proc.run([PY, "-c", "import time; time.sleep(60)"], timeout_s=2, check=True)


def test_streaming_child_yields_lines() -> None:
    script = "import sys\nfor i in range(3):\n    print(f'line {i}', flush=True)\n"
    with proc.start([PY, "-c", script]) as child:
        lines = list(child.lines())
        assert child.wait(timeout_s=30) == 0
    assert lines == ["line 0", "line 1", "line 2"]


def test_streaming_child_is_killed_on_exit() -> None:
    with proc.start([PY, "-c", "import time; time.sleep(120)"]) as child:
        pid = child.pid
        assert proc.pid_alive(pid)
    assert _wait_until_dead(pid)


def test_stderr_can_go_to_a_log_file(tmp_path: Path) -> None:
    log = tmp_path / "stderr.log"
    script = "import sys; print('out'); print('err', file=sys.stderr)"
    with proc.start([PY, "-c", script], stderr_to=log) as child:
        assert list(child.lines()) == ["out"]
        child.wait(timeout_s=30)
    assert log.read_text(encoding="utf-8").strip() == "err"


# ---- git integration: the regression this gateway exists for -----------------------------

_HOOK = """#!/bin/sh
# Simulates Entire's prepare-commit-msg: blocks unless ENTIRE_TEST_TTY=0.
if [ "$ENTIRE_TEST_TTY" != "0" ]; then sleep 120; fi
printf '\\nHook-Saw: ENTIRE_TEST_TTY=%s\\n' "$ENTIRE_TEST_TTY" >> "$1"
"""


@pytest.fixture
def scratch_repo(tmp_path: Path) -> Path:
    if proc.which("git") is None:
        pytest.skip("git is not installed")
    repo = tmp_path / "repo"
    proc.run(["git", "init", "-q", str(repo)], timeout_s=30, check=True)
    hook = repo / ".git" / "hooks" / "prepare-commit-msg"
    hook.write_text(_HOOK, encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    return repo


def _git(repo: Path, *args: str, timeout_s: float = 60) -> proc.Result:
    identity = ["-c", "user.name=Kintsugi Test", "-c", "user.email=test@example.invalid"]
    return proc.run(["git", "-C", str(repo), *identity, *args], timeout_s=timeout_s)


def test_commit_hooks_see_entire_test_tty_zero(scratch_repo: Path) -> None:
    result = _git(scratch_repo, "commit", "--allow-empty", "-q", "-m", "probe")
    assert result.ok, result.stderr
    message = _git(scratch_repo, "log", "-1", "--format=%B").stdout
    assert "Hook-Saw: ENTIRE_TEST_TTY=0" in message


def test_git_never_waits_for_an_editor(scratch_repo: Path) -> None:
    # No -m: git opens the editor. GIT_EDITOR=true returns at once instead of waiting for input.
    result = _git(scratch_repo, "commit", "--allow-empty", timeout_s=60)
    assert not result.timed_out
    assert result.duration_s < 30
