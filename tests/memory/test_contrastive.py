"""Contrastive precondition learning on real graphs (spec §6.7 step 19).

Positives: AB-1's origin before its fix (the practice ledger), the CRM practice host (the Clinic
stand-in) and the Ledger@pre-fix graph fixture. Negative: Scout, the disclosed look-alike. The
learner must keep all three positives, exclude Scout, and say why.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kintsugi.memory import contrastive as cl
from kintsugi.memory import motif as m
from kintsugi.memory import predicates
from kintsugi.memory.antibody import MotifSpec
from tests.helpers.graphs import host_graph, source_tree

V1_0 = MotifSpec.model_validate({
    "nodes": {
        "R": {"kind": "function", "pred": ["is_retry_wrapper"]},
        "Enc": {"kind": "function"},
        "F": {"kind": "function"},
        "S": {"kind": "class", "pred": ["has_mutable_accumulator"]},
    },
    "edges": [["Enc", "R", "CALLS"], ["Enc", "F", "CONTAINS"], ["F", "S", "CONSTRUCTS"]],
})  # fmt: skip
POSITIVES = {"ledger-practice": "ledger_practice_before", "crm-practice": "crm_practice",
             "ledger": "ledger_before"}  # fmt: skip


@pytest.fixture(scope="module")
def examples(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("graphs")
    matcher = m.MotifMatcher(V1_0, predicates=predicates.registry())

    def example(host: str, golden: str) -> cl.Example:
        g = host_graph(golden, source_tree(golden, Path(root) / golden))
        [embedding] = matcher.matches(g)
        return cl.Example(host, g, embedding)

    return {
        "positives": [example(h, g) for h, g in POSITIVES.items()],
        "negatives": [example("scout", "scout")],
    }


def test_the_learner_keeps_three_positives_and_excludes_scout(examples: dict[str, Any]) -> None:
    learned = cl.learn(V1_0, examples["positives"], examples["negatives"])
    assert learned.separated
    assert learned.selected == ("side_effect_in_scope",)
    assert learned.preconditions == ("a side-effecting callee inside the retried scope",)
    # The other candidates would cost protection: each fails on a positive.
    assert learned.admissible == ("side_effect_in_scope",)
    assert learned.table["side_effect_in_scope"] == {
        "ledger-practice": True, "crm-practice": True, "ledger": True, "scout": False}  # fmt: skip
    assert not learned.table["accumulator_has_cursor"]["ledger"]
    assert not learned.table["scope_returns_state"]["ledger"]
    assert set(learned.neighbourhoods) == {*POSITIVES, "scout"}
    assert all(size > 4 for size in learned.neighbourhoods.values())


def test_the_refined_motif_is_v1_1_and_passes_the_auditors_check(
    examples: dict[str, Any],
) -> None:
    learned = cl.learn(V1_0, examples["positives"], examples["negatives"])
    assert learned.motif.nodes["T"].pred == ["is_side_effecting"]
    assert ("F", "T", "CALLS*") in learned.motif.edges
    assert set(V1_0.edges) <= set(learned.motif.edges)
    registry = predicates.registry()
    problems = cl.verify(learned, examples["positives"], examples["negatives"],
                         predicates=registry)  # fmt: skip
    assert problems == []
    # The check is real: v1.0 itself fails it on Scout.
    unrefined = cl.Learned((), {}, (), V1_0, (), False, {})
    problems = cl.verify(unrefined, examples["positives"], examples["negatives"],
                         predicates=registry)  # fmt: skip
    assert problems == ["scout: the refined motif still matches a negative"]


def test_a_negative_no_admissible_candidate_excludes_is_reported(
    examples: dict[str, Any],
) -> None:
    # Treat a vulnerable host as the "negative": nothing admissible separates it.
    positives, negatives = examples["positives"][:1], examples["positives"][1:2]
    learned = cl.learn(V1_0, positives, negatives)
    assert not learned.separated
    assert learned.selected == ()


def test_resume_cursors_are_read_from_source() -> None:
    g = m.build_graph([m.Symbol("s", "class", "S", attrs={
        "source": "class S:\n    def __init__(self):\n        self.cursor = 0\n"
                  "    def step(self):\n        self.cursor += 1\n"})], [])  # fmt: skip
    assert predicates.has_resume_cursor(g, "s")
    g.nodes["s"]["source"] = "class S:\n    def __init__(self):\n        self.hits = []\n"
    assert not predicates.has_resume_cursor(g, "s")
