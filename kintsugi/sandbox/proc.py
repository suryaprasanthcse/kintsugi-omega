"""The single subprocess gateway (docs/EXECUTION_PLAN.md §2d).

Every child process (git, entire, claude, uv, docker) starts here, so that:

- ``ENTIRE_TEST_TTY=0``, ``GIT_TERMINAL_PROMPT=0`` and ``GIT_EDITOR=true`` are always set and
  cannot be overridden. Entire's ``prepare-commit-msg`` hook can then never block on a hidden
  console prompt, and git never waits for credentials or an editor.
- The environment is built from an allowlist. A secret reaches a child only when the caller
  passes it explicitly (spec §5.10: no secrets inside sandboxes).
- stdin is closed unless input is given, so nothing waits on a terminal.
- Every child runs in its own process group with no console. A timeout kills the whole tree,
  **root first**: a killed ``git commit`` aborts instead of finishing without its
  ``Entire-Checkpoint:`` trailer once its hook dies.

An architecture test and ruff's banned-API rule (TID251) forbid ``subprocess`` anywhere else.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

FORCED_ENV: Mapping[str, str] = {
    "ENTIRE_TEST_TTY": "0",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_EDITOR": "true",
}

# Defaults a caller may override.
DEFAULT_ENV: Mapping[str, str] = {"PYTHONUTF8": "1"}

# Names are matched case-insensitively (Windows environment names are case-insensitive).
ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Process basics.
        "PATH", "HOME", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "SHELL",
        "USER", "LOGNAME", "TMPDIR", "TEMP", "TMP",
        # Windows basics.
        "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE",
        "USERNAME", "USERDOMAIN", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
        "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
        "COMMONPROGRAMFILES", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
        # Toolchain locations (docs/EXECUTION_PLAN.md §1).
        "UV_PYTHON_INSTALL_DIR", "ENTIRE_PLUGIN_DIR",
        "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR",
        # TLS trust stores.
        "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    }
)  # fmt: skip

DEFAULT_TIMEOUT_S = 120.0
_DRAIN_TIMEOUT_S = 10.0


class ProcError(RuntimeError):
    """A child exited non-zero, or was killed, while the caller required success."""

    def __init__(self, message: str, result: Result) -> None:
        super().__init__(message)
        self.result = result


class ProcTimeout(ProcError):
    """A child exceeded its timeout; its whole process tree was killed."""


@dataclass(frozen=True)
class Result:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def check(self) -> Result:
        if self.timed_out:
            raise ProcTimeout(f"timed out after {self.duration_s:.1f}s: {self.argv}", self)
        if self.returncode != 0:
            tail = self.stderr.strip()[-2000:]
            raise ProcError(f"exit {self.returncode}: {self.argv}\n{tail}", self)
        return self


def build_env(
    extra: Mapping[str, str] | None = None,
    *,
    inherit: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the environment for a child: allowlisted inheritance, defaults, extras, forced."""
    source = os.environ if inherit is None else inherit
    env = {key: value for key, value in source.items() if key.upper() in ENV_ALLOWLIST}
    env.update(DEFAULT_ENV)
    for key, value in (extra or {}).items():
        forced = _forced_value(key)
        if forced is not None and value != forced:
            raise ValueError(f"{key} is forced to {forced!r} by the gateway and cannot be changed")
        env[key] = value
    env.update(FORCED_ENV)
    return env


def which(name: str) -> str | None:
    """Locate an executable on the gateway's PATH."""
    return shutil.which(name, path=build_env().get("PATH"))


def run(
    argv: Sequence[str | os.PathLike[str]],
    *,
    cwd: str | os.PathLike[str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    extra_env: Mapping[str, str] | None = None,
    input_text: str | None = None,
    check: bool = False,
) -> Result:
    """Run a command to completion and capture its output."""
    args = _normalise_argv(argv)
    flags, new_session = _group_flags()
    started = time.monotonic()
    popen = subprocess.Popen(
        args,
        cwd=cwd,
        env=build_env(extra_env),
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=flags,
        start_new_session=new_session,
    )
    timed_out = False
    try:
        stdout, stderr = popen.communicate(input=input_text, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(popen.pid)
        try:
            stdout, stderr = popen.communicate(timeout=_DRAIN_TIMEOUT_S)
        except subprocess.TimeoutExpired:  # a descendant escaped the group and holds a pipe
            popen.kill()
            stdout, stderr = "", "output not drained: a descendant held the pipe open"
    returncode = popen.returncode if popen.returncode is not None else -9
    result = Result(
        argv=tuple(args),
        returncode=returncode,
        stdout=stdout or "",
        stderr=stderr or "",
        duration_s=time.monotonic() - started,
        timed_out=timed_out,
    )
    return result.check() if check else result


class Proc:
    """A long-running child whose stdout is consumed line by line (e.g. ``claude -p``)."""

    def __init__(self, popen: subprocess.Popen[str], argv: tuple[str, ...]) -> None:
        self._popen = popen
        self.argv = argv
        self.started = time.monotonic()

    @property
    def pid(self) -> int:
        return self._popen.pid

    def lines(self) -> Iterator[str]:
        """Yield stdout lines without their line endings, until the child closes stdout."""
        stream = self._popen.stdout
        if stream is None:
            return
        for line in stream:
            yield line.rstrip("\r\n")

    def poll(self) -> int | None:
        return self._popen.poll()

    def wait(self, timeout_s: float | None = None) -> int | None:
        """Wait for exit; return the code, or ``None`` if the timeout passed first."""
        try:
            return self._popen.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return None

    def kill_tree(self) -> None:
        if self._popen.poll() is None:
            kill_tree(self._popen.pid)
        self._popen.wait(timeout=_DRAIN_TIMEOUT_S)

    def __enter__(self) -> Proc:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.kill_tree()
        if self._popen.stdout is not None:
            self._popen.stdout.close()


def start(
    argv: Sequence[str | os.PathLike[str]],
    *,
    cwd: str | os.PathLike[str] | None = None,
    extra_env: Mapping[str, str] | None = None,
    stderr_to: str | os.PathLike[str] | None = None,
) -> Proc:
    """Start a long-running child. stderr goes to ``stderr_to`` or is merged into stdout."""
    args = _normalise_argv(argv)
    flags, new_session = _group_flags()
    stderr_target = open(stderr_to, "ab") if stderr_to is not None else subprocess.STDOUT  # noqa: SIM115
    try:
        popen = subprocess.Popen(
            args,
            cwd=cwd,
            env=build_env(extra_env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=stderr_target,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=flags,
            start_new_session=new_session,
        )
    finally:
        if stderr_to is not None and not isinstance(stderr_target, int):
            stderr_target.close()  # the child holds its own handle
    return Proc(popen, tuple(args))


def kill_tree(pid: int) -> None:
    """Kill ``pid`` and all its descendants: the root first, then the rest."""
    if sys.platform == "win32":
        descendants = _win_descendants(pid)  # snapshot before the root dies
        _win_terminate(pid)
        for child in descendants:
            _win_terminate(child)
    else:
        import signal

        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGKILL)  # each child leads its own session (setsid)


def pid_alive(pid: int) -> bool:
    """Whether a process with this PID is still running."""
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a zombie still answers signal 0; check /proc where available
        status = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return True
    return "\nState:\tZ" not in status


def _forced_value(key: str) -> str | None:
    for name, value in FORCED_ENV.items():
        if name.upper() == key.upper():
            return value
    return None


def _normalise_argv(argv: Sequence[str | os.PathLike[str]]) -> list[str]:
    if isinstance(argv, str):
        raise TypeError("argv must be a sequence of arguments, not a shell string")
    args = [os.fspath(arg) for arg in argv]
    if not args:
        raise ValueError("argv is empty")
    return args


def _group_flags() -> tuple[int, bool]:
    """(creationflags, start_new_session): a new process group with no console."""
    if sys.platform == "win32":
        return subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW, False
    return 0, True


_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_TH32CS_SNAPPROCESS = 0x00000002


def _win_terminate(pid: int) -> None:
    if sys.platform != "win32":
        return
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
    if handle:
        try:
            kernel32.TerminateProcess(handle, 1)
        finally:
            kernel32.CloseHandle(handle)


def _win_descendants(root: int) -> list[int]:
    """All descendants of ``root`` in breadth-first order, from a Toolhelp32 snapshot."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [  # ctypes layout
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot in (None, wintypes.HANDLE(-1).value):
        return []
    children: dict[int, list[int]] = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            children.setdefault(entry.th32ParentProcessID, []).append(entry.th32ProcessID)
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    order: list[int] = []
    queue = [root]
    seen = {root}
    while queue:
        for child in children.get(queue.pop(0), []):
            if child not in seen:
                seen.add(child)
                order.append(child)
                queue.append(child)
    return order
