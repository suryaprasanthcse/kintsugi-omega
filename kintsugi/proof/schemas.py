"""Load and apply the Appendix E JSON Schemas (``schemas/E1–E6``).

The schemas are the contract between the crew, the proof plane and ``kintsugi audit``. In a clone
they are read from ``schemas/``; a wheel carries them as ``kintsugi/_schemas``.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

SCHEMAS: dict[str, str] = {
    "E1": "E1_antibody.schema.json",
    "E2": "E2_ledger_entry.schema.json",
    "E3": "E3_proof_bundle.schema.json",
    "E4": "E4_repair_receipt.schema.json",
    "E5": "E5_sabotage_commitment.schema.json",
    "E6": "E6_session_attestation.schema.json",
}
RECEIPT_TEMPLATE = "E4_repair_receipt.template.md"


def schema_dir() -> Path:
    packaged = Path(__file__).resolve().parents[1] / "_schemas"
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[2] / "schemas"


@cache
def load(name: str) -> dict[str, Any]:
    return json.loads((schema_dir() / SCHEMAS[name]).read_text(encoding="utf-8"))


def errors(name: str, instance: Any) -> list[str]:
    """Human-readable validation errors; empty when ``instance`` conforms."""
    validator = Draft202012Validator(load(name))
    return [
        f"{'/'.join(str(p) for p in error.absolute_path) or '<root>'}: {error.message}"
        for error in sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path))
    ]


def receipt_template() -> str:
    return (schema_dir() / RECEIPT_TEMPLATE).read_text(encoding="utf-8")
