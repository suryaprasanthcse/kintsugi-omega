from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from kintsugi.memory.antibody import (
    LIFECYCLE,
    Antibody,
    AntibodyState,
    AntibodyVersion,
    Change,
    MotifSpec,
)

E1_YAML = (Path(__file__).parents[1] / "fixtures" / "spec" / "E1_antibody.yaml").read_text(
    encoding="utf-8"
)
SCOUT_PRECONDITION = "side_effecting callee inside retried scope"


def _ab17() -> Antibody:
    return Antibody.from_yaml(E1_YAML)


def test_the_spec_example_parses() -> None:
    ab = _ab17()
    assert ab.ref == "AB-17@1.1"
    assert ab.state is AntibodyState.ACTIVE
    assert ab.receptor.type == "condition"
    assert ab.motif.nodes["S"].pred == ["has_mutable_accumulator"]
    assert ("F", "T", "CALLS") in ab.motif.edges
    assert ab.contract.canary["p1"] == "2*p0"
    assert ab.lineage.hosts_protected == ["ledger", "clinic-a", "oss-1"]


def test_yaml_round_trip() -> None:
    ab = _ab17()
    assert Antibody.from_yaml(ab.to_yaml()) == ab


@pytest.mark.parametrize(
    ("text", "parsed", "shown"),
    [("1.0", (1, 0, 0), "1.0"), ("1.1", (1, 1, 0), "1.1"), ("2.3.4", (2, 3, 4), "2.3.4")],
)
def test_versions(text: str, parsed: tuple[int, int, int], shown: str) -> None:
    version = AntibodyVersion.parse(text)
    assert (version.major, version.minor, version.patch) == parsed
    assert str(version) == shown


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("recipe", "2.0"),
        ("motif", "1.2"),
        ("precondition", "1.2"),
        ("receptor_threshold", "1.1.1"),
    ],
)
def test_version_bump_rules(change: Change, expected: str) -> None:
    assert str(AntibodyVersion.parse("1.1").bump(change)) == expected


def test_versions_order() -> None:
    v10, v11, v20 = (AntibodyVersion.parse(text) for text in ("1.0", "1.1", "2.0"))
    assert v10 < v11 < v20


def test_l3_quarantine_refine_reactivate() -> None:
    # Spec §6.7: AB-17@1.0 quarantined after the Scout canary rejects; refined to 1.1; re-matured.
    fresh_lineage = _ab17().lineage.model_copy(update={"versions": ["1.0"], "quarantines": 0})
    ab = _ab17().model_copy(update={"version": "1.0", "lineage": fresh_lineage})
    quarantined = ab.transition(AntibodyState.QUARANTINED)
    assert quarantined.lineage.quarantines == 1
    refined = quarantined.revise("precondition", preconditions=[SCOUT_PRECONDITION])
    assert refined.ref == "AB-17@1.1"
    assert refined.state is AntibodyState.QUARANTINED  # still needs re-maturation
    assert refined.lineage.versions == ["1.0", "1.1"]
    assert refined.transition(AntibodyState.ACTIVE).state is AntibodyState.ACTIVE


def test_illegal_lifecycle_transitions_are_refused() -> None:
    candidate = _ab17().model_copy(update={"state": AntibodyState.CANDIDATE})
    with pytest.raises(ValueError, match="not a lifecycle transition"):
        candidate.transition(AntibodyState.ACTIVE)  # must mature first
    retired = _ab17().transition(AntibodyState.RETIRED)
    for state in AntibodyState:
        with pytest.raises(ValueError, match="not a lifecycle transition"):
            retired.transition(state)
    with pytest.raises(ValueError, match="retired"):
        retired.revise("motif")


def test_every_state_can_reach_retired() -> None:
    for state, successors in LIFECYCLE.items():
        if state is not AntibodyState.RETIRED:
            assert AntibodyState.RETIRED in successors


@pytest.mark.parametrize("bad_id", ["AB17", "ab-17", "AB-", "XB-1"])
def test_antibody_ids_are_validated(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        Antibody.from_yaml(E1_YAML.replace("id: AB-17", f"id: {bad_id}"))


def test_motif_edges_must_use_declared_nodes() -> None:
    spec = {"nodes": {"F": {"kind": "function"}}, "edges": [["F", "X", "CALLS"]]}
    with pytest.raises(ValidationError, match="undeclared"):
        MotifSpec.model_validate(spec)
