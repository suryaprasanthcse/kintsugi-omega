"""The entire-graph adapter on real v0.4.0 output (golden snapshots pinned by spike A9)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kintsugi.memory import graph
from kintsugi.memory import motif as m
from kintsugi.memory.antibody import MotifSpec

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "graph"
TOOL_REGISTRY = {"post_journal_entry"}  # Ledger's side-effecting tool (spec §5.9.1)


def _snapshot(name: str) -> graph.Snapshot:
    text = (FIXTURES / "snapshots" / f"{name}.ndjson").read_text(encoding="utf-8")
    return graph.parse_snapshot(text.splitlines())


def _code_graph(name: str) -> Any:
    return _snapshot(name).to_code_graph(
        side_effect_tools=TOOL_REGISTRY, source_root=FIXTURES / name
    )


_CORE: dict[str, Any] = {
    "nodes": {
        "R": {"kind": "function", "pred": ["is_retry_wrapper"]},
        "Enc": {"kind": "function"},
        "F": {"kind": "function"},
        "S": {"kind": "class", "pred": ["has_mutable_accumulator"]},
    },
    "edges": [["Enc", "R", "CALLS"], ["Enc", "F", "CONTAINS"], ["F", "S", "CONSTRUCTS"]],
}
V1_0 = m.MotifMatcher(MotifSpec.model_validate(_CORE))
V1_1 = m.MotifMatcher(
    MotifSpec.model_validate(
        {
            "nodes": {**_CORE["nodes"], "T": {"kind": "function", "pred": ["is_side_effecting"]}},
            "edges": [*_CORE["edges"], ["F", "T", "CALLS*"]],
        }
    )
)


def test_header_and_summary() -> None:
    snapshot = _snapshot("ledger_before")
    assert snapshot.header["schema_version"] == "1.1"
    assert snapshot.header["provider_version"] == "v0.4.0"
    assert snapshot.summary["stats"]["completeness_level"] == "ok"
    assert snapshot.summary["completeness"]["languages"] == {"Python": {"files": 3, "symbols": 9}}


def test_nested_closure_is_a_contained_function() -> None:
    snapshot = _snapshot("ledger_before")
    attempt = snapshot.symbol("attempt")
    assert attempt["kind"] == "function"
    assert attempt["container_id"] == snapshot.symbol("run_reconcile")["id"]


def _relations(snapshot: graph.Snapshot, kind: str) -> set[tuple[str, str]]:
    names = {s["id"]: s["qualified_name"] for s in snapshot.symbols}
    return {
        (names[r["from_id"]], names[r["to_id"]])
        for r in snapshot.relations
        if r["type"] == kind and r["from_id"] in names and r["to_id"] in names
    }


def test_closure_constructions_are_also_credited_to_the_enclosing_function() -> None:
    # The finding that makes the motif bind F through CONTAINS (graph.py docstring).
    constructs = _relations(_snapshot("ledger_before"), "CONSTRUCTS")
    assert ("attempt", "ReconcileSession") in constructs
    assert ("run_reconcile", "ReconcileSession") in constructs
    after = _relations(_snapshot("ledger_after"), "CONSTRUCTS")
    assert ("attempt", "ReconcileSession") not in after
    assert ("run_reconcile", "ReconcileSession") in after


def test_reconciled_v1_0_motif_on_real_graphs() -> None:
    [embedding] = V1_0.matches(_code_graph("ledger_before"))
    names = {node: _code_graph("ledger_before").nodes[gid]["qualified_name"]
             for node, gid in embedding.items()}  # fmt: skip
    assert names == {
        "R": "retry_with_backoff",
        "Enc": "run_reconcile",
        "F": "attempt",
        "S": "ReconcileSession",
    }
    assert not V1_0.matches_any(_code_graph("ledger_after"))
    assert V1_0.matches_any(_code_graph("scout"))  # the disclosed look-alike over-matches


def test_v1_1_side_effect_precondition_on_real_graphs() -> None:
    before = _code_graph("ledger_before")
    assert V1_1.matches_any(before)  # attempt →CALLS→ ReconcileSession.post →CALLS→ tool
    assert not V1_1.matches_any(_code_graph("ledger_after"))
    assert not V1_1.matches_any(_code_graph("scout"))


def test_mutable_accumulator_is_read_from_real_source() -> None:
    before = _code_graph("ledger_before")
    [session] = [
        node for node, data in before.nodes(data=True)
        if data.get("qualified_name") == "ReconcileSession"
    ]  # fmt: skip
    assert m.has_mutable_accumulator(before, session)


def test_strict_parser_rejects_malformed_streams() -> None:
    lines = (FIXTURES / "snapshots" / "ledger_before.ndjson").read_text().splitlines()
    with pytest.raises(graph.GraphSchemaError, match="empty"):
        graph.parse_snapshot([])
    with pytest.raises(graph.GraphSchemaError, match="no summary"):
        graph.parse_snapshot(lines[:-1])
    broken = json.loads(next(line for line in lines if '"record_type": "symbol"' in line))
    del broken["kind"]
    with pytest.raises(graph.GraphSchemaError, match="lacks"):
        graph.parse_snapshot([lines[0], json.dumps(broken), lines[-1]])
    with pytest.raises(graph.GraphSchemaError, match="unknown record_type"):
        graph.parse_snapshot([lines[0], '{"record_type": "mystery"}', lines[-1]])
    header = json.loads(lines[0]) | {"schema_version": "2.0"}
    with pytest.raises(graph.GraphSchemaError, match="unsupported"):
        graph.parse_snapshot([json.dumps(header), lines[-1]])


def test_normalisation_is_idempotent_and_machine_independent() -> None:
    text = (FIXTURES / "snapshots" / "scout.ndjson").read_text(encoding="utf-8")
    assert graph.normalise_snapshot(text) == text
    header = json.loads(text.splitlines()[0])
    placeholders = (header["repo_root"], header["commit"], header["tree"])
    assert placeholders == ("<repo>", "<commit>", "<tree>")


def test_verify_requires_exactly_one_baseline_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        graph.verify(tmp_path, "pytest -q")
    with pytest.raises(ValueError, match="exactly one"):
        graph.verify(tmp_path, "pytest -q", record_baseline="a", pre_edit_baseline="b")
