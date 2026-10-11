"""Repair targets: where a host's code lives and how the gates check it (spec §5.9, §5.11).

A repository that Omega repairs declares its hosts in ``.kintsugi/target.yaml`` (a protected
path, so no Surgeon can change how it is judged):

.. code-block:: yaml

    version: 1
    hosts:
      ledger:
        host_yaml: hosts/ledger/host.yaml   # the host spec: services, tools, economics, SLO
        package_root: hosts/ledger          # put first on PYTHONPATH: the code under test
        tests: hosts/ledger/tests           # G3's suite
        lint: [hosts/ledger]                # G1: ruff
        typecheck: [hosts/ledger]           # G1: pyright
        holdout: hosts/ledger/ledger/evals/holdout/tasks.jsonl.age
        stress_profile: null                # a chaos profile for disputes and G7's G2
        live_services: []                   # services holding the host's own state: a cassette
                                            # never records or replays them (amendment A-50)

Gates evaluate a **checkout** (a detached worktree at one commit), never the working tree an
agent edited: ``code_env(checkout)`` points the host's eval runner at that checkout's code. The
host's mock services and offline model are the world, not the code under repair, so they are
imported from the target's own repository.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from kintsugi.fleet.hosts import HostSpec, load_host
from kintsugi.sandbox import chaos_proxy as chaos

TARGET_FILE = Path(".kintsugi/target.yaml")


class TargetError(ValueError):
    pass


@dataclass(frozen=True)
class Target:
    repo: Path
    host: HostSpec
    package_root: str
    tests: str
    lint: tuple[str, ...]
    typecheck: tuple[str, ...]
    holdout: str | None = None
    stress_profile: str | None = None
    install: tuple[str, ...] = ()  # optional G1 install command (e.g. "uv sync --locked")
    live_services: tuple[str, ...] = ()  # services holding the host's own state (A-50)

    @property
    def id(self) -> str:
        return self.host.id

    def code_env(self, checkout: Path) -> dict[str, str]:
        return {"PYTHONPATH": str((checkout / self.package_root).resolve())}

    def stress(self) -> chaos.Profile | None:
        if self.stress_profile is None:
            return None
        return chaos.load_profile(self.repo / self.stress_profile)

    def make_importable(self) -> None:
        """The host's services and offline model resolve in this process (Fabric imports them)."""
        root = str((self.repo / self.package_root).resolve())
        if root not in sys.path:
            sys.path.append(root)  # after site-packages: an installed host package still wins


def load_target(repo: Path, host: str) -> Target:
    repo = Path(repo)
    path = repo / TARGET_FILE
    if not path.is_file():
        raise TargetError(f"{repo} has no {TARGET_FILE.as_posix()}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data.get("version") != 1:
        raise TargetError(f"{path}: unsupported version")
    entry = (data.get("hosts") or {}).get(host)
    if entry is None:
        raise TargetError(f"{path}: no host {host!r}")
    spec = load_host(repo / entry["host_yaml"])
    return Target(
        repo=repo,
        host=spec,
        package_root=str(entry["package_root"]),
        tests=str(entry["tests"]),
        lint=tuple(entry.get("lint") or ()),
        typecheck=tuple(entry.get("typecheck") or ()),
        holdout=entry.get("holdout"),
        stress_profile=entry.get("stress_profile"),
        install=tuple(entry.get("install") or ()),
        live_services=tuple(entry.get("live_services") or ()),
    )


def target_hosts(repo: Path) -> list[str]:
    path = Path(repo) / TARGET_FILE
    if not path.is_file():
        return []
    return sorted(yaml.safe_load(path.read_text(encoding="utf-8")).get("hosts") or {})
