from __future__ import annotations

from pathlib import Path
from typing import Any

from kintsugi.proof import bundle as b

# Appendix E.3, verbatim (including its "…" placeholders).
E3_EXAMPLE: dict[str, Any] = {
    "incident": "INC-0042",
    "host": "ledger",
    "sealed_sabotage": {"id": "S-17", "commitment": "…"},
    "traces": {"failing": ["…"], "before": ["…"], "after": ["…"], "canary": ["…"]},
    "cassette": {"path": "cas/INC-0042.yaml", "sha256": "…", "g0": "3/3 fail"},
    "cause": {
        "suspects": ["run_reconcile", "ReconcileSession.__init__"],
        "graph_outputs": ["…"],
        "intent_record": {"checkpoint": "ck_7f3a", "quotes": ["…"]},
        "first_bad_checkpoint": "ck_7f3a",
    },
    "rounds": [{"hyps": [{"id": "H1", "checkpoint": "ck_…", "gates": {"G5": "warn"}}, "…"]}],
    "dispute": {"protocol": "…", "result": "tie → cost → H2'"},
    "winner": {
        "hyp": "H2'",
        "checkpoint": "ck_…",
        "stats": {"b": 26, "c": 1, "p": 2.1e-7, "cost_delta_ub": -0.004, "p95_delta_ub": 0.01},
    },
    "reverify": {"G3": "rerun:7", "G4": "rerun:intersection{run_reconcile}"},
    "ship": {"issue": 12, "pr": 13, "g7": "…"},
    "canary": {"p0": 0.06, "p1": 0.12, "path": [0.693, "…"], "decision": "accept", "n": 46},
    "cost": {"total_usd": 9.84, "by_role": {"…": 0}},
    "antibody": "AB-17@1.0",
}


def test_the_spec_example_is_a_valid_bundle() -> None:
    parsed = b.ProofBundle.model_validate(E3_EXAMPLE)
    assert parsed.as_json() == E3_EXAMPLE


def test_digest_on_disk_equals_digest_of_the_object(tmp_path: Path) -> None:
    bundle = b.ProofBundle.model_validate(E3_EXAMPLE)
    digest = b.write_bundle(tmp_path, bundle)
    loaded, on_disk = b.read_bundle(tmp_path)
    assert digest == on_disk == b.bundle_sha256(E3_EXAMPLE)
    assert loaded == bundle


def test_any_byte_change_changes_the_digest(tmp_path: Path) -> None:
    digest = b.write_bundle(tmp_path, b.ProofBundle.model_validate(E3_EXAMPLE))
    path = tmp_path / b.BUNDLE_FILE
    path.write_bytes(path.read_bytes().replace(b"9.84", b"9.85"))
    assert b.read_bundle(tmp_path)[1] != digest


def test_attachment_tampering_is_detected(tmp_path: Path) -> None:
    (tmp_path / "traces").mkdir()
    (tmp_path / "traces" / "before.json").write_text('{"trace": 1}')
    (tmp_path / "cassette.yaml").write_text("steps: []\n")
    b.write_bundle(tmp_path, b.ProofBundle(incident="INC-1", host="ledger"))
    assert b.verify_attachments(tmp_path) == []
    (tmp_path / "cassette.yaml").write_text("steps: [forged]\n")
    (tmp_path / "traces" / "before.json").unlink()
    (tmp_path / "extra.txt").write_text("added later")
    assert b.verify_attachments(tmp_path) == ["cassette.yaml", "extra.txt", "traces/before.json"]


def test_minimal_bundle_omits_empty_sections() -> None:
    minimal = b.ProofBundle(incident="INC-1", host="scout").as_json()
    assert minimal == {"incident": "INC-1", "host": "scout", "traces": {}, "rounds": []}
