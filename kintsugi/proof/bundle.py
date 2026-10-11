"""Proof bundles (spec §7.4, Appendix E.3).

A bundle is stored as its canonical JSON bytes, so the SHA-256 of ``bundle.json`` on disk equals
the SHA-256 of the canonical object. That digest is what the incident's ledger entry records.
Attachments (trace exports, the cassette, graph outputs, gate reports) are listed by digest.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from kintsugi.proof.canonical import canonicalize

BUNDLE_FILE = "bundle.json"
MANIFEST_FILE = "attachments.json"


class ProofBundle(BaseModel):
    """Appendix E.3. Later stages fill more sections; unknown sections are preserved."""

    model_config = ConfigDict(extra="allow")

    incident: str
    host: str
    sealed_sabotage: dict[str, Any] | None = None
    traces: dict[str, list[str]] = Field(default_factory=dict)
    cassette: dict[str, Any] | None = None
    cause: dict[str, Any] | None = None
    rounds: list[dict[str, Any]] = Field(default_factory=list)
    dispute: dict[str, Any] | None = None
    winner: dict[str, Any] | None = None
    reverify: dict[str, str] | None = None
    ship: dict[str, Any] | None = None
    canary: dict[str, Any] | None = None
    cost: dict[str, Any] | None = None
    antibody: str | None = None

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


def bundle_bytes(bundle: ProofBundle | Mapping[str, Any]) -> bytes:
    data = bundle.as_json() if isinstance(bundle, ProofBundle) else dict(bundle)
    return canonicalize(data)


def bundle_sha256(bundle: ProofBundle | Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(bundle_bytes(bundle)).hexdigest()


def file_sha256(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def write_bundle(directory: str | os.PathLike[str], bundle: ProofBundle) -> str:
    """Write ``bundle.json`` and the attachment manifest; return the bundle digest."""
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    (root / BUNDLE_FILE).write_bytes(bundle_bytes(bundle))
    manifest = attachment_manifest(root)
    (root / MANIFEST_FILE).write_bytes(canonicalize(manifest))
    return bundle_sha256(bundle)


def read_bundle(directory: str | os.PathLike[str]) -> tuple[ProofBundle, str]:
    """Load a bundle and the digest of its on-disk bytes."""
    path = Path(directory) / BUNDLE_FILE
    return ProofBundle.model_validate(json.loads(path.read_bytes())), file_sha256(path)


def attachment_manifest(directory: str | os.PathLike[str]) -> dict[str, str]:
    root = Path(directory)
    return {
        path.relative_to(root).as_posix(): file_sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name not in (BUNDLE_FILE, MANIFEST_FILE)
    }


def verify_attachments(directory: str | os.PathLike[str]) -> list[str]:
    """Paths whose bytes no longer match the recorded manifest (missing, changed or new)."""
    root = Path(directory)
    recorded: dict[str, str] = json.loads((root / MANIFEST_FILE).read_bytes())
    current = attachment_manifest(root)
    return sorted(
        name for name in set(recorded) | set(current) if recorded.get(name) != current.get(name)
    )
