"""Appendix E schemas: valid, they accept the spec's examples, and they agree with the code."""

from __future__ import annotations

import dataclasses
import json
import re
import string
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from kintsugi.memory.antibody import Antibody
from kintsugi.proof import attest, schemas, sealed
from kintsugi.proof.bundle import ProofBundle
from kintsugi.proof.ledger import Ledger, genesis_hash
from kintsugi.proof.passphrase import new_honeytoken, new_nonce

FIXTURES = Path(__file__).parents[1] / "fixtures" / "spec"
HEX64 = "ab" * 32
SHA40 = "c299a26ce100db907c1b32dd24e7fccb9faefc23"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_bytes())


@pytest.mark.parametrize("name", sorted(schemas.SCHEMAS))
def test_every_schema_is_valid_draft_2020_12(name: str) -> None:
    Draft202012Validator.check_schema(schemas.load(name))


# ---- E.1 ------------------------------------------------------------------------------------


def test_e1_spec_example_and_model_dump_conform() -> None:
    antibody = Antibody.from_yaml((FIXTURES / "E1_antibody.yaml").read_text(encoding="utf-8"))
    assert schemas.errors("E1", antibody.model_dump(mode="json")) == []


def test_e1_rejects_malformed_motif_edges() -> None:
    antibody = Antibody.from_yaml((FIXTURES / "E1_antibody.yaml").read_text(encoding="utf-8"))
    data = antibody.model_dump(mode="json")
    data["motif"]["edges"].append(["F", "S"])
    assert any("motif/edges" in error for error in schemas.errors("E1", data))


# ---- E.2 ------------------------------------------------------------------------------------


def test_e2_entries_from_the_ledger_conform() -> None:
    ledger = Ledger(genesis_hash("2026-10-12T00:00:00Z", bytes(32)))
    ledger.append({"type": "incident.opened", "at": "T0+02:24", "incident": "INC-0042"})
    entry = ledger.append(
        {"type": "incident.resolved", "at": "T0+03:51", "incident": "INC-0042",
         "bundle_sha256": f"sha256:{HEX64}", "cost_usd": 9.84}
    )  # fmt: skip
    assert schemas.errors("E2", entry) == []


def test_e2_spec_example_fails_only_on_its_symbolic_hashes() -> None:
    example = _fixture("E2_ledger_entry.json")
    failing = {error.split(":")[0] for error in schemas.errors("E2", example)}
    assert failing == {"bundle_sha256", "plan_sha256", "prev", "hash"}
    example.update(prev=HEX64, hash=HEX64, bundle_sha256=f"sha256:{HEX64}",
                   plan_sha256=f"sha256:{HEX64}")  # fmt: skip
    assert schemas.errors("E2", example) == []


# ---- E.3 ------------------------------------------------------------------------------------


def test_e3_spec_example_and_model_dump_conform() -> None:
    example = _fixture("E3_proof_bundle.json")
    assert schemas.errors("E3", example) == []
    assert schemas.errors("E3", ProofBundle.model_validate(example).as_json()) == []


def test_e3_requires_incident_and_host() -> None:
    assert schemas.errors("E3", {"incident": "INC-1"}) == ["<root>: 'host' is a required property"]


# ---- E.4 ------------------------------------------------------------------------------------


def test_e4_template_placeholders_match_the_schema() -> None:
    parsed = string.Formatter().parse(schemas.receipt_template())
    fields = {name for _, name, _, _ in parsed if name}
    assert fields == set(schemas.load("E4")["properties"])
    assert fields == set(schemas.load("E4")["required"])


def test_e4_template_renders() -> None:
    values: dict[str, Any] = {name: f"<{name}>" for name in schemas.load("E4")["properties"]}
    values.update(INC="INC-0042", seq=812, issue=12)
    assert schemas.errors("E4", values) == []
    body = schemas.receipt_template().format_map(values)
    assert body.startswith("## Kintsugi repair receipt — INC-0042")
    assert body.rstrip().endswith("Fixes #12")
    assert not re.search(r"\{[a-zA-Z_]+\}", body)


# ---- E.5 ------------------------------------------------------------------------------------


def _s17() -> dict[str, Any]:
    return {
        "id": "S-17", "class": "code.regression.state_reset_on_retry",
        "target": {"host": "ledger"}, "delivery": {"mode": "claude_code_session"},
        "ground_truth": {"files": ["ledger/reconcile.py"]}, "schedule": {"jitter_s": 1800},
        "honeytoken": new_honeytoken(), "seed": 918273,
    }  # fmt: skip


def test_e5_records_from_the_code_conform() -> None:
    spec = _s17()
    record = sealed.commitment_record(spec, sealed.seal(spec), sealed_at="T0+00:05",
                                      not_before="T0+02:00")  # fmt: skip
    signed = {**record, "attestation": "ed25519:" + "A" * 86 + "=="}
    assert schemas.errors("E5", record) == []
    assert schemas.errors("E5", signed) == []


def test_e5_forbids_extra_fields_so_nothing_leaks() -> None:
    spec = _s17()
    record = sealed.commitment_record(spec, sealed.seal(spec), sealed_at="t", not_before="t")
    leaky = {**record, "honeytoken": spec["honeytoken"]}
    assert schemas.errors("E5", leaky) != []


def test_e5_spec_example_fails_only_on_its_placeholders() -> None:
    example = _fixture("E5_sabotage_commitment.json")
    failing = {error.split(":")[0] for error in schemas.errors("E5", example)}
    assert failing == {"c", "attestation"}


# ---- E.6 ------------------------------------------------------------------------------------


def _signed_attestation() -> attest.SessionAttestation:
    unsigned = attest.SessionAttestation(
        nonce=new_nonce(), role="surgeon", hypothesis="H2", incident="INC-0042",
        worktree="wt/INC-0042/H2", base_sha=SHA40, prompt_template_sha=f"sha256:{HEX64}",
        t_start="2026-10-12T03:14:07Z",
    )  # fmt: skip
    return attest.attest(unsigned, attest.Ed25519Signer.generate(), tree_sha=SHA40,
                         parent_sha=SHA40)  # fmt: skip


def test_e6_signed_attestations_conform() -> None:
    assert schemas.errors("E6", dataclasses.asdict(_signed_attestation())) == []


def test_e6_spec_example_hex_nonce_is_superseded_by_n1() -> None:
    errors = schemas.errors("E6", _fixture("E6_session_attestation.json"))
    assert any(error.startswith("nonce:") for error in errors)


def test_e6_rejects_hex_nonces() -> None:
    data = dataclasses.asdict(_signed_attestation())
    data["nonce"] = "9f2c4e1a" * 4
    assert [e.split(":")[0] for e in schemas.errors("E6", data)] == ["nonce"]
