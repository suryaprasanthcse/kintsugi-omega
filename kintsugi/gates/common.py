"""The shape every gate returns (spec §5.11): a status and the evidence behind it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Status = Literal["pass", "fail", "warn"]
GATES = ("G0", "G1", "G2", "G3", "G4", "G5", "G6")


@dataclass(frozen=True)
class GateResult:
    gate: str
    status: Status
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """``warn`` passes the gate but ranks the hypothesis down (G5, spec §6.3 H1)."""
        return self.status != "fail"

    def as_dict(self) -> dict[str, Any]:
        return {"gate": self.gate, "status": self.status, "summary": self.summary,
                "evidence": self.evidence}  # fmt: skip


def passed(gate: str, summary: str, **evidence: Any) -> GateResult:
    return GateResult(gate, "pass", summary, evidence)


def failed(gate: str, summary: str, **evidence: Any) -> GateResult:
    return GateResult(gate, "fail", summary, evidence)


def warned(gate: str, summary: str, **evidence: Any) -> GateResult:
    return GateResult(gate, "warn", summary, evidence)
