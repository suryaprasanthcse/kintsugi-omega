"""Session attestation and commit trailers (spec §4.5, Appendix E.6).

The orchestrator signs ``sha256(nonce ‖ tree_sha ‖ parent_sha ‖ role ‖ incident ‖
prompt_template_sha)`` for every commit an agent session produces. The concatenation ``‖`` is
realised as the RFC 8785 encoding of the six fields as a JSON array, so field boundaries are
unambiguous.

Keys: local Ed25519 now; an AWS KMS ``ECC_NIST_P256`` signer can implement :class:`Signer` in
Stage 8. The private key never enters the repository or any agent's context: the T0 key is
generated under the Architect's custody (``kintsugi keys generate``) and only the OpenSSH public
key is committed to ``.kintsugi/attest.pub``.

Commit identity (ruling D4): every commit is authored and committed by the Architect's git
identity; machine origin is proven by these trailers, never by a bot account or a
``Co-authored-by:`` trailer, which :func:`commit_message_problems` rejects.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from kintsugi.proof.canonical import canonicalize
from kintsugi.proof.passphrase import is_nonce

TRAILER_KEYS = (
    "Kintsugi-Incident",
    "Kintsugi-Role",
    "Kintsugi-Nonce",
    "Kintsugi-Prompt",
    "Kintsugi-Attestation",
)
ENTIRE_TRAILER = "Entire-Checkpoint"
_TRAILER_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9-]*):\s?(.*)$")
_COAUTHOR = re.compile(r"^\s*co-authored-by\s*:", re.IGNORECASE | re.MULTILINE)


class Signer(Protocol):
    algorithm: str

    def sign(self, digest: bytes) -> bytes: ...

    def public_key_openssh(self) -> str: ...


class Ed25519Signer:
    algorithm = "ed25519"

    def __init__(self, key: Ed25519PrivateKey) -> None:
        self._key = key

    @classmethod
    def generate(cls) -> Ed25519Signer:
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def load(cls, private_key_path: str | os.PathLike[str]) -> Ed25519Signer:
        key = serialization.load_ssh_private_key(Path(private_key_path).read_bytes(), None)
        if not isinstance(key, Ed25519PrivateKey):
            raise TypeError("the attestation key must be Ed25519")
        return cls(key)

    def sign(self, digest: bytes) -> bytes:
        return self._key.sign(digest)

    def public_key_raw(self) -> bytes:
        return self._key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )

    def public_key_openssh(self, comment: str = "kintsugi-omega-attest") -> str:
        key = self._key.public_key().public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
        )
        return f"{key.decode('ascii')} {comment}"

    def save(
        self, private_key_path: str | os.PathLike[str], public_key_path: str | os.PathLike[str]
    ) -> None:
        private_path = Path(private_key_path)
        if private_path.exists():
            raise FileExistsError(f"refusing to overwrite an existing key: {private_path}")
        private_path.parent.mkdir(parents=True, exist_ok=True)
        pem = self._key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
        descriptor = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(pem)
        public_path = Path(public_key_path)
        public_path.parent.mkdir(parents=True, exist_ok=True)
        public_path.write_text(self.public_key_openssh() + "\n", encoding="ascii", newline="\n")


def load_public_key(text_or_path: str | os.PathLike[str]) -> Ed25519PublicKey:
    """Load an OpenSSH Ed25519 public key from its text or from a file such as attest.pub."""
    raw = os.fspath(text_or_path)
    text = raw if raw.startswith("ssh-ed25519 ") else Path(raw).read_text(encoding="ascii")
    key = serialization.load_ssh_public_key(text.strip().encode("ascii"))
    if not isinstance(key, Ed25519PublicKey):
        raise TypeError("the attestation key must be Ed25519")
    return key


def fingerprint(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


@dataclass(frozen=True)
class SessionAttestation:
    """Appendix E.6. ``tree_sha``, ``parent_sha`` and ``signature`` are filled at commit time."""

    nonce: str
    role: str
    incident: str
    worktree: str
    base_sha: str
    prompt_template_sha: str
    t_start: str
    hypothesis: str | None = None
    tree_sha: str | None = None
    parent_sha: str | None = None
    signature: str | None = None

    @property
    def role_label(self) -> str:
        return f"{self.role}/{self.hypothesis}" if self.hypothesis else self.role


def signing_digest(
    nonce: str,
    tree_sha: str,
    parent_sha: str,
    role: str,
    incident: str,
    prompt_template_sha: str,
) -> bytes:
    fields = [nonce, tree_sha, parent_sha, role, incident, prompt_template_sha]
    return hashlib.sha256(canonicalize(fields)).digest()


def _digest_for(att: SessionAttestation) -> bytes:
    if att.tree_sha is None or att.parent_sha is None:
        raise ValueError("tree_sha and parent_sha are required to sign or verify")
    return signing_digest(
        att.nonce, att.tree_sha, att.parent_sha, att.role_label, att.incident,
        att.prompt_template_sha,
    )  # fmt: skip


def attest(
    att: SessionAttestation, signer: Signer, *, tree_sha: str, parent_sha: str
) -> SessionAttestation:
    if not is_nonce(att.nonce):
        raise ValueError("the nonce must be a 128-bit passphrase (spike N1)")
    filled = replace(att, tree_sha=tree_sha, parent_sha=parent_sha)
    signature = base64.b64encode(signer.sign(_digest_for(filled))).decode("ascii")
    return replace(filled, signature=f"{signer.algorithm}:{signature}")


def verify_attestation(att: SessionAttestation, public_key: Ed25519PublicKey) -> bool:
    if att.signature is None or not att.signature.startswith("ed25519:"):
        return False
    try:
        signature = base64.b64decode(att.signature.removeprefix("ed25519:"), validate=True)
        public_key.verify(signature, _digest_for(att))
    except (InvalidSignature, ValueError):
        return False
    return True


def trailers(att: SessionAttestation) -> list[tuple[str, str]]:
    if att.signature is None:
        raise ValueError("sign the attestation before writing trailers")
    values = [att.incident, att.role_label, att.nonce, att.prompt_template_sha, att.signature]
    return list(zip(TRAILER_KEYS, values, strict=True))


def format_trailers(pairs: list[tuple[str, str]]) -> str:
    return "\n".join(f"{key}: {value}" for key, value in pairs)


def parse_trailers(message: str) -> dict[str, list[str]]:
    """Trailers from the trailing trailer-only paragraphs of a commit message.

    Consecutive trailing paragraphs that consist only of ``Key: value`` lines are merged, so the
    orchestrator's ``Kintsugi-*`` block and an ``Entire-Checkpoint:`` line that a hook appends
    in its own paragraph are read together. The subject line is never a trailer.
    """
    paragraphs = [block for block in re.split(r"\n\s*\n", message.strip()) if block.strip()]
    parsed: dict[str, list[str]] = {}
    for block in reversed(paragraphs[1:]):
        matches = [_TRAILER_LINE.match(line.strip()) for line in block.splitlines()]
        if not all(matches):
            break  # prose: the trailer section ends here
        for match in reversed(matches):
            assert match is not None
            parsed.setdefault(match.group(1), []).insert(0, match.group(2).strip())
    return parsed


def commit_message_problems(message: str, *, require_attestation: bool) -> list[str]:
    """Policy check run before every commit the orchestrator makes."""
    problems: list[str] = []
    if _COAUTHOR.search(message):
        problems.append("Co-authored-by trailers are forbidden (ruling D4)")
    if require_attestation:
        found = parse_trailers(message)
        missing = [key for key in TRAILER_KEYS if key not in found]
        if missing:
            problems.append(f"missing attestation trailers: {missing}")
    return problems
