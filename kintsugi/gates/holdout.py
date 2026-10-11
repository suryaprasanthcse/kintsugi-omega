"""Holdout custody (spec §5.11 anti-gaming, invariant I5, audit check C13).

- The holdout is generated from a **secret** seed drawn here, so it cannot be regenerated from
  the host's public generator; the seed exists only inside the encrypted payload.
- Every holdout task embeds the host's **canary** (a passphrase, so it survives Entire's
  redaction like a honeytoken): any appearance of it in crew context, transcripts or spans is a
  leak (I5). Only its SHA-256 is public.
- The payload is age-encrypted to the recipient in ``.kintsugi/holdout.pub``; the identity
  (private key) lives outside every repository and is released only to the G4 activity.
- ``.kintsugi/holdout.sha256`` lists ``sha256  path`` of each sealed file plus the canary
  digests; it is committed at T0 and C13 checks it is unchanged through the window.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyrage

from kintsugi.proof.canonical import canonicalize
from kintsugi.proof.passphrase import new_honeytoken
from kintsugi.proof.sealed import new_identity

MANIFEST = Path(".kintsugi/holdout.sha256")
RECIPIENT = Path(".kintsugi/holdout.pub")
FORMAT = "kintsugi.holdout/v1"


class HoldoutError(RuntimeError):
    pass


def holdout_file(package: str, root: Path) -> Path:
    return root / package / "evals" / "holdout" / "tasks.jsonl.age"


def _inside_git_worktree(path: Path) -> bool:
    return any((parent / ".git").exists() for parent in path.resolve().parents)


def keygen(identity_path: Path, repo: Path) -> str:
    """Write a fresh age identity outside any repository; commit only the recipient."""
    if _inside_git_worktree(identity_path):
        raise HoldoutError(f"{identity_path} is inside a git work tree; keep the key out")
    if identity_path.exists():
        raise HoldoutError(f"{identity_path} exists; refusing to overwrite a holdout key")
    secret, recipient = new_identity()
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    identity_path.write_text(secret + "\n", encoding="ascii")
    os.chmod(identity_path, 0o600)
    (repo / RECIPIENT).write_text(recipient + "\n", encoding="ascii", newline="\n")
    return recipient


@dataclass(frozen=True)
class SealReport:
    host: str
    path: Path
    sha256: str
    tasks: int
    canary_sha256: str


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def seal(
    repo: Path, host: str, package: str, root: Path, *, recipient: str | None = None
) -> SealReport:
    """Generate the host's holdout from a secret seed, embed a canary, encrypt, record hashes."""
    recipient = recipient or (repo / RECIPIENT).read_text(encoding="ascii").strip()
    generator = importlib.import_module(f"{package}.evals.generate")
    canary = new_honeytoken()
    seed = secrets.randbits(62)
    tasks = generator.generate("holdout", seed, generator.SIZES["holdout"], canary=canary)
    payload = {"format": FORMAT, "host": host, "seed": str(seed),  # JCS caps ints at 2^53 - 1
               "canary": canary, "tasks": tasks}  # fmt: skip
    ciphertext = pyrage.encrypt(canonicalize(payload),
                                [pyrage.x25519.Recipient.from_str(recipient)])  # fmt: skip
    target = holdout_file(package, root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(ciphertext)
    report = SealReport(host, target, _sha256_file(target), len(tasks),
                        hashlib.sha256(canary.encode()).hexdigest())  # fmt: skip
    _record(repo, report)
    return report


def _record(repo: Path, report: SealReport) -> None:
    manifest = repo / MANIFEST
    relative = report.path.resolve().relative_to(repo.resolve()).as_posix()
    stale = (f"  {relative}", f" canary {report.host} ")
    kept = [line for line in _manifest_lines(manifest) if not any(s in line for s in stale)]
    kept += [
        f"{report.sha256}  {relative}",
        f"# canary {report.host} sha256:{report.canary_sha256}",
    ]
    header = "# KINTSUGI-Ω holdout manifest (spec §6.1): sha256 of each sealed holdout file."
    body = [line for line in kept if not line.startswith("# KINTSUGI")]
    manifest.write_text("\n".join([header, *sorted(body)]) + "\n", encoding="utf-8", newline="\n")


def _manifest_lines(manifest: Path) -> list[str]:
    if not manifest.is_file():
        return []
    return [line for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()]


def verify(repo: Path) -> list[str]:
    """C13 primitive: every listed file exists and hashes to its recorded digest."""
    problems = []
    entries = [line for line in _manifest_lines(repo / MANIFEST) if not line.startswith("#")]
    if not entries:
        problems.append(f"{MANIFEST} lists no sealed holdout")
    for line in entries:
        digest, _, relative = line.partition("  ")
        path = repo / relative
        if not path.is_file():
            problems.append(f"{relative}: missing")
        elif _sha256_file(path) != digest:
            problems.append(f"{relative}: hash changed")
    return problems


def open_holdout(path: Path, identity: str) -> dict[str, Any]:
    """Decrypt a sealed holdout (G4 only). Raises if the identity is wrong."""
    try:
        plaintext = pyrage.decrypt(path.read_bytes(), [pyrage.x25519.Identity.from_str(identity)])
    except pyrage.DecryptError as error:
        raise HoldoutError(f"{path.name}: cannot decrypt with this identity") from error
    payload = json.loads(plaintext)
    if payload.get("format") != FORMAT:
        raise HoldoutError(f"{path.name}: not a {FORMAT} payload")
    return payload


def tasks_jsonl(payload: dict[str, Any]) -> str:
    return "".join(json.dumps(task) + "\n" for task in payload["tasks"])


def canary_hits(blob: bytes, canary: str) -> int:
    """How many times a holdout canary appears in some captured context (I5 leak scan)."""
    return blob.count(canary.encode("utf-8"))
