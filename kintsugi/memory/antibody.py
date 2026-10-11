"""Antibodies (spec §5.8, Appendix E.1).

Lifecycle (§5.8.2)::

    CANDIDATE ──cross-host maturation passes──▶ MATURED ──receptor precision ≥ 0.9──▶ ACTIVE
    ACTIVE ──canary rejects on any host──▶ QUARANTINED ──refined + re-matured──▶ ACTIVE
    ACTIVE ──superseded / obsolete──▶ RETIRED

RETIRED is also reachable from CANDIDATE, MATURED and QUARANTINED, so an antibody that fails
maturation or refinement can always be closed out (docs/EXECUTION_PLAN.md, Amendments log).

Versioning: **major** when the recipe changes, **minor** when the motif or a precondition
changes (the refinement after a quarantine), **patch** when a receptor threshold changes.
Versions print as ``major.minor`` and add ``.patch`` only when it is non-zero ("1.0", "1.1").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ID = re.compile(r"^AB-\d+$")
_VERSION = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?$")


class AntibodyState(StrEnum):
    CANDIDATE = "CANDIDATE"
    MATURED = "MATURED"
    ACTIVE = "ACTIVE"
    QUARANTINED = "QUARANTINED"
    RETIRED = "RETIRED"


LIFECYCLE: dict[AntibodyState, frozenset[AntibodyState]] = {
    AntibodyState.CANDIDATE: frozenset({AntibodyState.MATURED, AntibodyState.RETIRED}),
    AntibodyState.MATURED: frozenset({AntibodyState.ACTIVE, AntibodyState.RETIRED}),
    AntibodyState.ACTIVE: frozenset({AntibodyState.QUARANTINED, AntibodyState.RETIRED}),
    AntibodyState.QUARANTINED: frozenset({AntibodyState.ACTIVE, AntibodyState.RETIRED}),
    AntibodyState.RETIRED: frozenset(),
}

Change = Literal["recipe", "motif", "precondition", "receptor_threshold"]


@dataclass(frozen=True, order=True)
class AntibodyVersion:
    major: int
    minor: int
    patch: int = 0

    @classmethod
    def parse(cls, text: str) -> AntibodyVersion:
        match = _VERSION.match(text)
        if match is None:
            raise ValueError(f"not an antibody version: {text!r}")
        return cls(int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))

    def bump(self, change: Change) -> AntibodyVersion:
        if change == "recipe":
            return AntibodyVersion(self.major + 1, 0, 0)
        if change in ("motif", "precondition"):
            return AntibodyVersion(self.major, self.minor + 1, 0)
        return AntibodyVersion(self.major, self.minor, self.patch + 1)

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}"
        return f"{base}.{self.patch}" if self.patch else base


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


class Signature(_Open):
    hash: str
    fields: dict[str, Any] = Field(default_factory=dict)


class Receptor(_Open):
    detection_id: str | None = None
    type: Literal["regex", "condition", "pii", "classifier"]
    rule: str
    precision_preview: float | None = None


class MotifNode(BaseModel):
    kind: str
    pred: list[str] = Field(default_factory=list)


class MotifPattern(BaseModel):
    """A forbidden sub-pattern (immune tolerance). It may reference the positive motif's nodes."""

    nodes: dict[str, MotifNode] = Field(default_factory=dict)
    edges: list[tuple[str, str, str]]


class MotifSpec(BaseModel):
    nodes: dict[str, MotifNode]
    edges: list[tuple[str, str, str]]
    negative: list[MotifPattern] = Field(default_factory=list)

    @model_validator(mode="after")
    def _edges_reference_declared_nodes(self) -> MotifSpec:
        declared = set(self.nodes)
        for source, target, _ in self.edges:
            if source not in declared or target not in declared:
                raise ValueError(f"motif edge {source}->{target} uses an undeclared node")
        for pattern in self.negative:
            known = declared | set(pattern.nodes)
            for source, target, _ in pattern.edges:
                if source not in known or target not in known:
                    raise ValueError(f"negative edge {source}->{target} uses an undeclared node")
        return self

    def relations(self) -> set[str]:
        used = {relation for _, _, relation in self.edges}
        for pattern in self.negative:
            used |= {relation for _, _, relation in pattern.edges}
        return used


class Contract(_Open):
    repro: str = "3/3"
    holdout: dict[str, Any] = Field(default_factory=dict)
    cost_delta_ub: float = 0.05
    p95_delta_ub: float = 0.10
    canary: dict[str, Any] = Field(default_factory=dict)


class Economics(_Open):
    primary_usd: float | None = None
    secondary_usd_mean: float | None = None


class Lineage(_Open):
    versions: list[str] = Field(default_factory=list)
    quarantines: int = 0
    hosts_protected: list[str] = Field(default_factory=list)


class Antibody(BaseModel):
    """Appendix E.1."""

    id: str
    version: str
    state: AntibodyState
    signature: Signature
    receptor: Receptor
    motif: MotifSpec
    preconditions: list[str] = Field(default_factory=list)
    recipe: str
    contract: Contract = Field(default_factory=Contract)
    evidence: dict[str, Any] = Field(default_factory=dict)
    economics: Economics = Field(default_factory=Economics)
    lineage: Lineage = Field(default_factory=Lineage)

    @field_validator("id")
    @classmethod
    def _id_format(cls, value: str) -> str:
        if not _ID.match(value):
            raise ValueError(f"antibody ids look like AB-17, not {value!r}")
        return value

    @field_validator("version")
    @classmethod
    def _version_format(cls, value: str) -> str:
        return str(AntibodyVersion.parse(value))

    @property
    def ref(self) -> str:
        """The pinned reference vaccination PRs use, e.g. ``AB-17@1.1``."""
        return f"{self.id}@{self.version}"

    def transition(self, to: AntibodyState) -> Antibody:
        if to not in LIFECYCLE[self.state]:
            raise ValueError(f"{self.ref}: {self.state} → {to} is not a lifecycle transition")
        lineage = self.lineage
        if to is AntibodyState.QUARANTINED:
            lineage = lineage.model_copy(update={"quarantines": lineage.quarantines + 1})
        return self.model_copy(update={"state": to, "lineage": lineage})

    def revise(self, change: Change, **updates: Any) -> Antibody:
        """A new version after ``change``; refinement of a quarantined antibody is a minor bump."""
        if self.state is AntibodyState.RETIRED:
            raise ValueError(f"{self.ref} is retired")
        version = str(AntibodyVersion.parse(self.version).bump(change))
        versions = [*self.lineage.versions, version]
        if self.version not in versions:
            versions.insert(0, self.version)
        lineage = self.lineage.model_copy(update={"versions": versions})
        return self.model_copy(update={**updates, "version": version, "lineage": lineage})

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False, allow_unicode=True)

    @classmethod
    def from_yaml(cls, text: str) -> Antibody:
        return cls.model_validate(yaml.safe_load(text))
