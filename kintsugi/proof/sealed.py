"""Sealed-sabotage commit-reveal (spec §7.2, Appendix E.5).

    c = SHA-256(JCS(spec) ‖ salt),   salt = 32 random bytes

The commitment record published before delivery carries only ``id``, ``c``, ``sealed_at``,
``not_before`` and a coarse ``class_family``. The spec and salt are stored age-encrypted, and
only the Saboteur activity holds the identity that decrypts them. The reveal publishes both.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pyrage

from kintsugi.proof.canonical import canonicalize

SALT_BYTES = 32
SPEC_FIELDS = (
    "id",
    "class",
    "target",
    "delivery",
    "ground_truth",
    "schedule",
    "honeytoken",
    "seed",
)


@dataclass(frozen=True)
class Seal:
    commitment: str
    salt: bytes


def commitment(spec: Mapping[str, Any], salt: bytes) -> str:
    if len(salt) != SALT_BYTES:
        raise ValueError(f"salt must be {SALT_BYTES} bytes")
    return "sha256:" + hashlib.sha256(canonicalize(spec) + salt).hexdigest()


def seal(spec: Mapping[str, Any], *, salt: bytes | None = None) -> Seal:
    check_spec_shape(spec)
    chosen = secrets.token_bytes(SALT_BYTES) if salt is None else salt
    return Seal(commitment=commitment(spec, chosen), salt=chosen)


def verify_reveal(published: str, spec: Mapping[str, Any], salt: bytes) -> bool:
    try:
        recomputed = commitment(spec, salt)
    except ValueError:
        return False
    return hmac.compare_digest(published, recomputed)


def check_spec_shape(spec: Mapping[str, Any]) -> None:
    missing = [name for name in SPEC_FIELDS if name not in spec]
    if missing:
        raise ValueError(f"sabotage spec is missing {missing}")


def class_family(sabotage_class: str) -> str:
    """The coarse family published before reveal: the class without its last component."""
    parts = sabotage_class.split(".")
    if len(parts) < 2 or not all(parts):
        raise ValueError(f"not a catalog class: {sabotage_class!r}")
    return ".".join(parts[:-1])


def commitment_record(
    spec: Mapping[str, Any], sealed: Seal, *, sealed_at: str, not_before: str
) -> dict[str, str]:
    """The public record (E.5, before its attestation is added)."""
    return {
        "id": str(spec["id"]),
        "c": sealed.commitment,
        "sealed_at": sealed_at,
        "not_before": not_before,
        "class_family": class_family(str(spec["class"])),
    }


def reveal_record(spec: Mapping[str, Any], sealed: Seal) -> dict[str, Any]:
    return {"spec": dict(spec), "salt": sealed.salt.hex()}


def new_identity() -> tuple[str, str]:
    """A fresh age X25519 identity: (secret ``AGE-SECRET-KEY-1…``, recipient ``age1…``)."""
    identity = pyrage.x25519.Identity.generate()
    return str(identity), str(identity.to_public())


def encrypt_sealed(spec: Mapping[str, Any], sealed: Seal, recipients: Sequence[str]) -> bytes:
    if not recipients:
        raise ValueError("at least one age recipient is required")
    payload = canonicalize({"spec": dict(spec), "salt": sealed.salt.hex()})
    keys = [pyrage.x25519.Recipient.from_str(recipient) for recipient in recipients]
    return pyrage.encrypt(payload, keys)


def decrypt_sealed(ciphertext: bytes, identity: str) -> tuple[dict[str, Any], Seal]:
    payload = json.loads(pyrage.decrypt(ciphertext, [pyrage.x25519.Identity.from_str(identity)]))
    spec = payload["spec"]
    salt = bytes.fromhex(payload["salt"])
    return spec, Seal(commitment=commitment(spec, salt), salt=salt)
