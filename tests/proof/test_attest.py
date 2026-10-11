from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from kintsugi.proof import attest
from kintsugi.proof.passphrase import new_nonce

TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
PARENT = "c299a26ce100db907c1b32dd24e7fccb9faefc23"


def _attestation() -> attest.SessionAttestation:
    return attest.SessionAttestation(
        nonce=new_nonce(),
        role="surgeon",
        hypothesis="H2",
        incident="INC-0042",
        worktree="wt/INC-0042/H2",
        base_sha=PARENT,
        prompt_template_sha="sha256:" + "ab" * 32,
        t_start="2026-10-12T03:14:07Z",
    )


@pytest.fixture
def signer() -> attest.Ed25519Signer:
    return attest.Ed25519Signer.generate()


def test_sign_and_verify(signer: attest.Ed25519Signer) -> None:
    signed = attest.attest(_attestation(), signer, tree_sha=TREE, parent_sha=PARENT)
    assert signed.signature is not None
    assert signed.signature.startswith("ed25519:")
    public = attest.load_public_key(signer.public_key_openssh())
    assert attest.verify_attestation(signed, public)


@pytest.mark.parametrize(
    "field",
    ["nonce", "tree_sha", "parent_sha", "role", "hypothesis", "incident", "prompt_template_sha"],
)
def test_any_signed_field_change_breaks_the_signature(
    signer: attest.Ed25519Signer, field: str
) -> None:
    signed = attest.attest(_attestation(), signer, tree_sha=TREE, parent_sha=PARENT)
    value = new_nonce() if field == "nonce" else "tampered"
    forged = replace(signed, **{field: value})
    public = attest.load_public_key(signer.public_key_openssh())
    assert not attest.verify_attestation(forged, public)


def test_a_different_key_does_not_verify(signer: attest.Ed25519Signer) -> None:
    signed = attest.attest(_attestation(), signer, tree_sha=TREE, parent_sha=PARENT)
    other = attest.load_public_key(attest.Ed25519Signer.generate().public_key_openssh())
    assert not attest.verify_attestation(signed, other)


def test_field_boundaries_are_unambiguous() -> None:
    # Plain concatenation would make ("ab", "c") and ("a", "bc") collide; the JCS array does not.
    a = attest.signing_digest("ab", "c", "p", "r", "i", "s")
    b = attest.signing_digest("a", "bc", "p", "r", "i", "s")
    assert a != b


def test_hex_nonces_are_refused(signer: attest.Ed25519Signer) -> None:
    hex_nonce = replace(_attestation(), nonce="9f2c4e1a" * 4)
    with pytest.raises(ValueError, match="passphrase"):
        attest.attest(hex_nonce, signer, tree_sha=TREE, parent_sha=PARENT)


def test_trailers_round_trip_through_a_commit_message(signer: attest.Ed25519Signer) -> None:
    signed = attest.attest(_attestation(), signer, tree_sha=TREE, parent_sha=PARENT)
    pairs = attest.trailers(signed)
    assert [key for key, _ in pairs] == list(attest.TRAILER_KEYS)
    message = (
        "fix(ledger): hoist ReconcileSession out of the retried closure\n\n"
        "Preserves resume semantics.\n\n"
        f"{attest.format_trailers(pairs)}\n"
        "Entire-Checkpoint: 01M4MXCTAM5YSYDX4HXMJ5V7H4\n"
    )
    parsed = attest.parse_trailers(message)
    assert parsed["Kintsugi-Role"] == ["surgeon/H2"]
    assert parsed["Kintsugi-Nonce"] == [signed.nonce]
    assert parsed[attest.ENTIRE_TRAILER] == ["01M4MXCTAM5YSYDX4HXMJ5V7H4"]
    assert attest.commit_message_problems(message, require_attestation=True) == []


def test_unsigned_attestations_have_no_trailers() -> None:
    with pytest.raises(ValueError, match="sign"):
        attest.trailers(_attestation())


@pytest.mark.parametrize(
    "line", ["Co-authored-by: X <x@y>", "co-authored-by: X", "CO-AUTHORED-BY:X"]
)
def test_co_author_trailers_are_rejected(line: str) -> None:
    message = f"docs: update\n\n{line}\n"
    problems = attest.commit_message_problems(message, require_attestation=False)
    assert problems == ["Co-authored-by trailers are forbidden (ruling D4)"]


def test_missing_attestation_trailers_are_listed() -> None:
    message = "fix: x\n\nKintsugi-Incident: INC-1\n"
    problems = attest.commit_message_problems(message, require_attestation=True)
    assert len(problems) == 1
    assert "Kintsugi-Nonce" in problems[0]


def test_a_body_paragraph_is_not_a_trailer_block() -> None:
    assert attest.parse_trailers("subject\n\nthis is prose, not: a trailer block at all\n") == {}
    assert attest.parse_trailers("subject only") == {}


def test_key_files_round_trip(tmp_path: Path, signer: attest.Ed25519Signer) -> None:
    private, public = tmp_path / "keys" / "attest_ed25519", tmp_path / "attest.pub"
    signer.save(private, public)
    assert public.read_text().startswith("ssh-ed25519 ")
    assert public.read_text().endswith("kintsugi-omega-attest\n")
    if sys.platform != "win32":
        assert (os.stat(private).st_mode & 0o777) == 0o600
    reloaded = attest.Ed25519Signer.load(private)
    assert reloaded.public_key_raw() == signer.public_key_raw()
    signed = attest.attest(_attestation(), reloaded, tree_sha=TREE, parent_sha=PARENT)
    assert attest.verify_attestation(signed, attest.load_public_key(public))
    with pytest.raises(FileExistsError):
        signer.save(private, public)


def test_fingerprint_format(signer: attest.Ed25519Signer) -> None:
    fp = attest.fingerprint(attest.load_public_key(signer.public_key_openssh()))
    assert fp.startswith("SHA256:")
    assert "=" not in fp
