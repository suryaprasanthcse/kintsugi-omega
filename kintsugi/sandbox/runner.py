"""Sandboxed execution of host code (spec §5.10, ruling D2).

Host tasks, evals and every untrusted Clinic step run in a Docker container under gVisor
(``--runtime runsc``) with:

- a non-root user, a read-only root filesystem and a size-capped ``tmpfs`` work directory;
- CPU, memory (no extra swap), PID and wall-clock limits; all capabilities dropped and
  ``no-new-privileges``;
- a network that is either ``none`` or an ``--internal`` network whose only way out is the
  egress gateway (``kintsugi.sandbox.egress``);
- **no secrets**: an environment variable whose name looks like a credential is refused unless
  its value is a ``kintsugi-proxy-`` placeholder that the proxies swap for the real credential.

On Windows the engine lives in WSL2 Ubuntu (D2), reached as ``wsl.exe -d Ubuntu --exec docker``;
every command still goes through the subprocess gateway.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from kintsugi.sandbox import proc

PROXY_PLACEHOLDER = "kintsugi-proxy-"
_SECRET_NAME = re.compile(r"(?i)(key|token|secret|passw|credential|auth)")
_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")


class SandboxError(RuntimeError):
    pass


@dataclass(frozen=True)
class Limits:
    cpus: float = 1.0
    memory_mb: int = 1024
    pids: int = 256
    wall_s: float = 900.0
    tmpfs_mb: int = 256


@dataclass(frozen=True)
class Mount:
    source: str  # a path as the Docker engine sees it
    target: str


@dataclass(frozen=True)
class SandboxSpec:
    name: str
    image: str
    command: tuple[str, ...]
    network: str = "none"
    runtime: str = "runsc"
    user: str = "65534:65534"  # nobody
    workdir: str = "/work"
    limits: Limits = Limits()
    env: Mapping[str, str] = field(default_factory=dict)
    mounts: tuple[Mount, ...] = ()
    # name → IP written to /etc/hosts. Under runsc, Docker's embedded DNS (127.0.0.11, an
    # iptables redirect in the container's namespace) does not answer, so the egress gateway is
    # addressed through a pinned hosts entry (spike N8).
    extra_hosts: Mapping[str, str] = field(default_factory=dict)


def check_env(env: Mapping[str, str]) -> None:
    """Refuse anything that looks like a credential (spec §5.10: no secrets inside sandboxes)."""
    for name, value in env.items():
        if _SECRET_NAME.search(name) and not value.startswith(PROXY_PLACEHOLDER):
            raise SandboxError(
                f"refusing to pass {name} into a sandbox: secrets stay in the proxies; "
                f"pass a {PROXY_PLACEHOLDER}… placeholder instead"
            )


def docker_run_argv(spec: SandboxSpec) -> list[str]:
    if not _NAME.match(spec.name):
        raise SandboxError(f"bad container name {spec.name!r}")
    check_env(spec.env)
    limits = spec.limits
    argv = [
        "docker", "run", "--rm", "--name", spec.name,
        "--runtime", spec.runtime,
        "--user", spec.user,
        "--read-only",
        "--tmpfs", f"{spec.workdir}:rw,size={limits.tmpfs_mb}m,mode=1777",
        "--tmpfs", "/tmp:rw,size=64m,mode=1777",
        "--workdir", spec.workdir,
        "--cpus", f"{limits.cpus:g}",
        "--memory", f"{limits.memory_mb}m",
        "--memory-swap", f"{limits.memory_mb}m",
        "--pids-limit", str(limits.pids),
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--network", spec.network,
    ]  # fmt: skip
    for mount in spec.mounts:
        argv += ["--mount", f"type=bind,source={mount.source},target={mount.target},readonly"]
    for name, value in sorted(spec.env.items()):
        argv += ["--env", f"{name}={value}"]
    for name, address in sorted(spec.extra_hosts.items()):
        argv += ["--add-host", f"{name}:{address}"]
    return [*argv, spec.image, *spec.command]


def container_ip(host: DockerHost, container: str, network: str) -> str:
    """The container's address on ``network`` (Docker 29 leaves the top-level field empty)."""
    template = '{{(index .NetworkSettings.Networks "' + network + '").IPAddress}}'
    address = host.docker("inspect", "-f", template, container).check().stdout.strip()
    if not address:
        raise SandboxError(f"{container} has no address on {network}")
    return address


@dataclass(frozen=True)
class DockerHost:
    """Where the Docker engine runs: natively, or inside a WSL2 distribution."""

    prefix: tuple[str, ...] = ()
    wsl_distro: str | None = None

    @classmethod
    def wsl(cls, distro: str = "Ubuntu") -> DockerHost:
        return cls(("wsl.exe", "-d", distro, "--exec"), distro)

    def argv(self, docker_argv: Sequence[str]) -> list[str]:
        return [*self.prefix, *docker_argv]

    def docker(self, *args: str, timeout_s: float = 120.0) -> proc.Result:
        return proc.run(self.argv(["docker", *args]), timeout_s=timeout_s)

    def engine_path(self, path: Path) -> str:
        """A host path as the engine sees it (C:\\x → /mnt/c/x inside WSL)."""
        if self.wsl_distro is None:
            return str(path)
        windows = PureWindowsPath(str(path))
        if not windows.drive:  # relative: resolve it (on Windows, where the engine is in WSL)
            windows = PureWindowsPath(str(path.resolve()))
        drive = windows.drive.rstrip(":").lower()
        return "/mnt/" + drive + "/" + "/".join(windows.parts[1:])

    def runtimes(self) -> set[str]:
        result = self.docker("info", "--format", "{{json .Runtimes}}", timeout_s=60)
        if not result.ok:
            return set()
        return set(json.loads(result.stdout or "{}"))

    @classmethod
    def detect(cls) -> DockerHost | None:
        """The local engine if it answers, else WSL2 Ubuntu's on Windows, else None."""
        candidates: list[DockerHost] = []
        if shutil.which("docker") and sys.platform != "win32":
            candidates.append(cls())
        if sys.platform == "win32" and shutil.which("wsl.exe"):
            candidates.append(cls.wsl())
        for host in candidates:
            try:
                if host.docker("version", "--format", "{{.Server.Version}}", timeout_s=60).ok:
                    return host
            except (OSError, proc.ProcError):
                continue
        return None


@dataclass(frozen=True)
class SandboxResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def run(host: DockerHost, spec: SandboxSpec) -> SandboxResult:
    """Run to completion. On the wall-clock limit the container itself is killed, not just the
    client (killing ``docker run`` alone would leave the container running)."""
    argv = host.argv(docker_run_argv(spec))
    result = proc.run(argv, timeout_s=spec.limits.wall_s)
    if result.timed_out:
        host.docker("kill", spec.name, timeout_s=60)
        host.docker("rm", "-f", spec.name, timeout_s=60)
    return SandboxResult(result.returncode, result.stdout, result.stderr, result.timed_out,
                         result.duration_s)  # fmt: skip
