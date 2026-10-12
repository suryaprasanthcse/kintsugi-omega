"""The committed Antibody Library: AB-1 as the Stage 7 rehearsal left it (l3-20261012-074523).

AB-1 was synthesised from the Stage 6 incident that resolved, matured on the CRM practice host,
quarantined after Scout's canary rejected the (disclosed, injected) literal vaccination, refined
contrastively and re-matured. Its files must load, its pinned versions resolve, and its v1.1
motif must keep the three positives and exclude Scout on the golden graphs.
"""

from __future__ import annotations

from pathlib import Path

from kintsugi.memory import motif as m
from kintsugi.memory import predicates
from kintsugi.memory.antibody import AntibodyState
from kintsugi.memory.library import AntibodyLibrary
from tests.helpers.graphs import host_graph, source_tree

OMEGA = Path(__file__).resolve().parents[2]
LIBRARY = AntibodyLibrary(OMEGA / "antibodies")


def test_ab1_is_active_at_1_1_with_its_history() -> None:
    ab = LIBRARY.load("AB-1")
    assert ab.ref == "AB-1@1.1"
    assert ab.state is AntibodyState.ACTIVE
    assert ab.lineage.versions == ["1.0", "1.1"]
    assert ab.lineage.quarantines == 1
    assert ab.preconditions == ["a side-effecting callee inside the retried scope"]
    assert LIBRARY.ref("AB-1@1.0").preconditions == []  # the quarantined version stays pinned
    assert LIBRARY.index()["signatures"][ab.signature.hash] == "AB-1@1.1"
    moves = [(e["from"], e["to"]) for e in LIBRARY.lineage("AB-1")["events"]
             if e["event"] == "state"]  # fmt: skip
    assert moves == [("CANDIDATE", "MATURED"), ("MATURED", "ACTIVE"), ("ACTIVE", "QUARANTINED"),
                     ("QUARANTINED", "ACTIVE")]  # fmt: skip
    [refinement] = LIBRARY.lineage("AB-1")["refinements"]
    assert refinement["negatives"] == ["scout"]
    assert refinement["revert_pr"].endswith("/pull/9")
    for name in ("receptor.json", "motif.yaml", "recipe.md", "contract.yaml", "evidence.json"):
        assert (OMEGA / "antibodies" / "AB-1" / name).is_file()
    for slot in ("{S}", "{F}", "{R}", "{Enc}"):
        assert slot in ab.recipe


def test_ab1_motifs_on_the_golden_graphs(tmp_path: Path) -> None:
    v10 = m.MotifMatcher(LIBRARY.ref("AB-1@1.0").motif, predicates=predicates.registry())
    v11 = m.MotifMatcher(LIBRARY.load("AB-1").motif, predicates=predicates.registry())
    for name in ("ledger_practice_before", "crm_practice", "ledger_before"):
        g = host_graph(name, source_tree(name, tmp_path / name))
        assert v10.matches_any(g), name
        assert v11.matches_any(g), name
    scout = host_graph("scout", source_tree("scout", tmp_path / "scout"))
    assert v10.matches_any(scout)  # the false match that caused the quarantine
    assert not v11.matches_any(scout)  # learned tolerance
