"""Hourly ledger anchors (spec §7.3, amendment A-67).

An anchor publishes the ledger head ``h_seq`` at the top of an hour, so a later rewrite of any
entry up to ``seq`` changes a hash that is already public (audit check C1). It is an annotated
git tag ``anchor-YYYYMMDD-HH`` (UTC) whose message is the canonical JSON

    {"h": h_seq, "seq": seq, "sig": "ed25519:<base64>", "tag": "anchor-YYYYMMDD-HH"}

where ``sig`` signs ``SHA-256(JCS({"h", "seq", "tag"}))`` with the T0 attestation key. Omega
writes the tag and the post text; the Architect posts the text (§7.3: the anchor post is the
one outward-facing step Omega does not take itself).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from kintsugi.proof.attest import Signer
from kintsugi.proof.canonical import canonicalize

TAG_PREFIX = "anchor-"
_TAG = re.compile(r"^anchor-\d{8}-\d{2}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def tag_name(at: datetime) -> str:
    """The tag for the hour containing ``at`` (UTC)."""
    return f"{TAG_PREFIX}{at.astimezone(UTC):%Y%m%d-%H}"


def anchor_digest(tag: str, seq: int, h: str) -> bytes:
    return hashlib.sha256(canonicalize({"h": h, "seq": seq, "tag": tag})).digest()


@dataclass(frozen=True)
class Anchor:
    tag: str
    seq: int
    h: str
    sig: str

    def as_dict(self) -> dict[str, Any]:
        return {"h": self.h, "seq": self.seq, "sig": self.sig, "tag": self.tag}

    def message(self) -> str:
        return canonicalize(self.as_dict()).decode("utf-8")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Anchor:
        return cls(str(data["tag"]), int(data["seq"]), str(data["h"]), str(data["sig"]))

    @classmethod
    def from_message(cls, tag: str, text: str) -> Anchor:
        """Parse a tag message; the tag name inside must equal the tag itself."""
        anchor = cls.from_dict(json.loads(text.strip()))
        if anchor.tag != tag:
            raise ValueError(f"tag {tag} carries the anchor of {anchor.tag}")
        return anchor


def sign_anchor(tag: str, seq: int, h: str, signer: Signer) -> Anchor:
    if not _TAG.match(tag):
        raise ValueError(f"not an anchor tag: {tag!r}")
    if seq < 0 or not _HEX64.match(h):
        raise ValueError("an anchor needs seq ≥ 0 and a 64-hex ledger hash")
    signature = base64.b64encode(signer.sign(anchor_digest(tag, seq, h))).decode("ascii")
    return Anchor(tag, seq, h, f"{signer.algorithm}:{signature}")


def verify_anchor(anchor: Anchor, public_key: Ed25519PublicKey) -> bool:
    if not _TAG.match(anchor.tag) or not anchor.sig.startswith("ed25519:"):
        return False
    try:
        signature = base64.b64decode(anchor.sig.removeprefix("ed25519:"), validate=True)
        public_key.verify(signature, anchor_digest(anchor.tag, anchor.seq, anchor.h))
    except (InvalidSignature, ValueError):
        return False
    return True


def post_text(anchor: Anchor, repo_url: str) -> str:
    """What the Architect posts each hour: enough to re-check the anchor from the post alone."""
    return (
        f"KINTSUGI-Ω ledger anchor {anchor.tag}\n"
        f"seq {anchor.seq}  h {anchor.h}\n"
        f"sig {anchor.sig}\n"
        f"verify: kintsugi audit --all (C1) · {repo_url}/tree/{anchor.tag}"
    )
