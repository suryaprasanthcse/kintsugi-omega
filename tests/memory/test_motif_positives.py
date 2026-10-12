"""The Stage 7 practice positives on their real graphs: AB-1's motif binds where the pathogen is.

``ledger_practice_before`` is AB-1's origin before its fix (the Stage 6 practice regression);
``crm_practice`` is the disclosed Clinic stand-in with the same latent defect in another domain.
Both bind the reconciled v1.0 motif and its v1.1 side-effect refinement; Scout, the disclosed
look-alike, binds only v1.0 (spec §6.7).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kintsugi.memory import motif as m
from kintsugi.memory import predicates
from kintsugi.memory.antibody import MotifSpec
from tests.helpers.graphs import host_graph, source_tree

CORE: dict[str, Any] = {
    "nodes": {
        "R": {"kind": "function", "pred": ["is_retry_wrapper"]},
        "Enc": {"kind": "function"},
        "F": {"kind": "function"},
        "S": {"kind": "class", "pred": ["has_mutable_accumulator"]},
    },
    "edges": [["Enc", "R", "CALLS"], ["Enc", "F", "CONTAINS"], ["F", "S", "CONSTRUCTS"]],
}
V1_1: dict[str, Any] = {
    "nodes": {**CORE["nodes"], "T": {"kind": "function", "pred": ["is_side_effecting"]}},
    "edges": [*CORE["edges"], ["F", "T", "CALLS*"]],
}


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ledger_practice_before",
         {"R": "retry_with_backoff", "Enc": "reconcile", "F": "book_pending_lines",
          "S": "PostingSession"}),
        ("crm_practice",
         {"R": "retry_with_backoff", "Enc": "sync_contacts", "F": "attempt",
          "S": "SyncSession"}),
    ],
)  # fmt: skip
def test_both_positives_bind_v1_0_and_v1_1(
    name: str, expected: dict[str, str], tmp_path: Path
) -> None:
    g = host_graph(name, source_tree(name, tmp_path / name))
    v10 = m.MotifMatcher(MotifSpec.model_validate(CORE), predicates=predicates.registry())
    v11 = m.MotifMatcher(MotifSpec.model_validate(V1_1), predicates=predicates.registry())
    [embedding] = v10.matches(g)
    assert predicates.names(g, embedding) == expected
    assert v11.matches_any(g)


def test_scout_binds_only_v1_0(tmp_path: Path) -> None:
    g = host_graph("scout", source_tree("scout", tmp_path / "scout"))
    assert m.MotifMatcher(MotifSpec.model_validate(CORE)).matches_any(g)
    assert not m.MotifMatcher(MotifSpec.model_validate(V1_1)).matches_any(g)
