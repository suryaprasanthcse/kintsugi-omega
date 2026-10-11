from __future__ import annotations

import hashlib
import json
from typing import Any

import pyrage
import pytest

from kintsugi.proof import sealed
from kintsugi.proof.canonical import canonicalize
from kintsugi.proof.passphrase import new_honeytoken


def _spec_s17() -> dict[str, Any]:
    # Spec §6.2 step 1, with a passphrase honeytoken (spike N1).
    return {
        "id": "S-17",
        "class": "code.regression.state_reset_on_retry",
        "target": {"host": "ledger"},
        "delivery": {
            "mode": "claude_code_session",
            "framing": "Refactor run_reconcile retry logic for readability",
            "patch_sha256": "sha256:3d1f",
        },
        "ground_truth": {
            "files": ["ledger/reconcile.py"],
            "symbols": ["run_reconcile", "ReconcileSession.__init__"],
            "mechanism": "ReconcileSession constructed inside the retried closure",
        },
        "schedule": {"not_before": "T0+02:00", "jitter_s": 1800},
        "honeytoken": new_honeytoken(),
        "seed": 918273,
    }


def test_commitment_is_sha256_of_jcs_spec_then_salt() -> None:
    spec, salt = _spec_s17(), bytes(range(32))
    expected = hashlib.sha256(canonicalize(spec) + salt).hexdigest()
    assert sealed.commitment(spec, salt) == f"sha256:{expected}"


def test_seal_then_reveal_verifies() -> None:
    spec = _spec_s17()
    seal = sealed.seal(spec)
    assert len(seal.salt) == 32
    assert sealed.verify_reveal(seal.commitment, spec, seal.salt)


def test_reveal_fails_for_a_changed_spec_or_salt() -> None:
    spec = _spec_s17()
    seal = sealed.seal(spec)
    changed = {**spec, "seed": 1}
    assert not sealed.verify_reveal(seal.commitment, changed, seal.salt)
    assert not sealed.verify_reveal(seal.commitment, spec, bytes(32))
    assert not sealed.verify_reveal(seal.commitment, spec, b"short")


def test_salts_are_fresh() -> None:
    spec = _spec_s17()
    assert sealed.seal(spec).salt != sealed.seal(spec).salt


def test_public_record_reveals_nothing_about_the_fault() -> None:
    spec = _spec_s17()
    seal = sealed.seal(spec)
    record = sealed.commitment_record(spec, seal, sealed_at="T0+00:05", not_before="T0+02:00")
    assert set(record) == {"id", "c", "sealed_at", "not_before", "class_family"}
    assert record["class_family"] == "code.regression"
    published = json.dumps(record)
    for secret in [spec["honeytoken"], "ReconcileSession", "reconcile.py", "state_reset"]:
        assert secret not in published


@pytest.mark.parametrize(
    ("cls", "family"),
    [
        ("code.regression.state_reset_on_retry", "code.regression"),
        ("env.rate_limit", "env"),
        ("healer.kill_worker", "healer"),
    ],
)
def test_class_family_is_coarse(cls: str, family: str) -> None:
    assert sealed.class_family(cls) == family


def test_class_family_rejects_non_catalog_names() -> None:
    with pytest.raises(ValueError, match="catalog"):
        sealed.class_family("regression")


def test_incomplete_specs_cannot_be_sealed() -> None:
    spec = _spec_s17()
    del spec["honeytoken"]
    with pytest.raises(ValueError, match="honeytoken"):
        sealed.seal(spec)


def test_age_storage_round_trip_and_key_scoping() -> None:
    spec = _spec_s17()
    seal = sealed.seal(spec)
    saboteur_secret, saboteur_public = sealed.new_identity()
    other_secret, _ = sealed.new_identity()
    ciphertext = sealed.encrypt_sealed(spec, seal, [saboteur_public])
    assert spec["honeytoken"].encode() not in ciphertext
    revealed_spec, revealed_seal = sealed.decrypt_sealed(ciphertext, saboteur_secret)
    assert revealed_spec == spec
    assert revealed_seal == seal
    with pytest.raises(pyrage.DecryptError):
        sealed.decrypt_sealed(ciphertext, other_secret)


def test_reveal_record_lets_anyone_check_the_commitment() -> None:
    spec = _spec_s17()
    seal = sealed.seal(spec)
    reveal = json.loads(json.dumps(sealed.reveal_record(spec, seal)))
    assert sealed.verify_reveal(seal.commitment, reveal["spec"], bytes.fromhex(reveal["salt"]))
