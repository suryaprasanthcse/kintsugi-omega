from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kintsugi.proof import bundle as b

# Appendix E.3, verbatim (including its "…" placeholders).
E3_EXAMPLE: dict[str, Any] = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "spec" / "E3_proof_bundle.json").read_bytes()
)


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
