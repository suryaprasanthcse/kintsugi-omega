"""The real fleet carries the code shapes the cascade relies on (Stage 3 verification).

Each host is snapshotted with the live entire-graph as its own repository, then:

- the healthy fleet: the reconciled v1.1 motif matches **nothing** (no false positives), while
  v1.0 over-matches exactly Scout's disclosed look-alike (spec §5.9.2, §6.7);
- Ledger has every precondition of S-17 (spec §6.2): ``run_reconcile`` calls
  ``retry_with_backoff`` and contains ``attempt``, ``ReconcileSession`` holds a mutable
  accumulator, ``attempt`` transitively reaches the side-effecting ``post_journal_entry``, and
  the session is constructed by ``run_reconcile`` but not by ``attempt``. One extra
  ``attempt -CONSTRUCTS-> ReconcileSession`` edge (constructing it inside the retried scope)
  is all it takes for v1.1 to match, at graph level, without writing the patch anywhere.

Run with ``KINTSUGI_CONTRACT=1``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import networkx as nx
import pytest

from kintsugi.fleet.hosts import load_hosts
from kintsugi.memory import graph
from kintsugi.memory import motif as m
from kintsugi.memory.antibody import MotifSpec

from .scratch import make_repo

pytestmark = pytest.mark.contract

REPO = Path(__file__).resolve().parents[2]
HOSTS = load_hosts(REPO)

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


@pytest.fixture(scope="module")
def graphs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, nx.DiGraph]:
    if os.environ.get("KINTSUGI_CONTRACT") != "1":
        pytest.skip("contract tests need KINTSUGI_CONTRACT=1")
    built = {}
    for host, spec in HOSTS.items():
        repo = make_repo(tmp_path_factory.mktemp("fleet") / host, spec.root / spec.package)
        text = graph._run(["snapshot", "--format", "ndjson", "--profile", "full", "--worktree"],
                          repo)  # fmt: skip
        snapshot = graph.parse_snapshot(text.splitlines())
        built[host] = snapshot.to_code_graph(side_effect_tools=spec.side_effecting_tools(),
                                             source_root=repo)  # fmt: skip
    return built


def _node(g: nx.DiGraph, name: str) -> str:
    [found] = [n for n, data in g.nodes(data=True) if data.get("qualified_name") == name]
    return found


def _rels(g: nx.DiGraph, a: str, b: str) -> set[str]:
    return set(g[a][b]["rels"]) if g.has_edge(a, b) else set()


def test_the_healthy_fleet_has_no_v1_1_match(graphs: dict[str, nx.DiGraph]) -> None:
    assert {host for host, g in graphs.items() if V1_1.matches_any(g)} == set()


def test_v1_0_over_matches_only_scouts_disclosed_look_alike(graphs: dict[str, nx.DiGraph]) -> None:
    assert {host for host, g in graphs.items() if V1_0.matches_any(g)} == {"scout"}
    [embedding] = V1_0.matches(graphs["scout"])
    names = {k: graphs["scout"].nodes[v]["qualified_name"] for k, v in embedding.items()}
    assert names == {"R": "retry_with_backoff", "Enc": "fetch_all_pages", "F": "attempt",
                     "S": "ScratchBuffer"}  # fmt: skip


def test_ledger_has_every_s17_precondition(graphs: dict[str, nx.DiGraph]) -> None:
    g = graphs["ledger"]
    run, retry = _node(g, "run_reconcile"), _node(g, "retry_with_backoff")
    session = _node(g, "ReconcileSession")
    [attempt] = [n for n in g.successors(run) if "CONTAINS" in _rels(g, run, n)
                 and g.nodes[n].get("name") == "attempt"]  # fmt: skip
    post = _node(g, "ReconcileSession.post")
    assert "CALLS" in _rels(g, run, retry)
    assert "CONSTRUCTS" in _rels(g, run, session)
    assert "CONSTRUCTS" not in _rels(g, attempt, session)
    assert "CALLS*" in _rels(g, post, _node(g, "post_journal_entry"))
    assert m.has_mutable_accumulator(g, session)
    assert not V1_1.matches_any(g)
    # Constructing the session inside the retried scope gives `attempt` a CONSTRUCTS edge and,
    # with the local type now known, a CALLS edge to ReconcileSession.post (as on the Stage 2
    # ledger_before fixture). Those two edges alone complete the v1.1 motif.
    sabotaged = g.copy()
    for target, relation in ((session, "CONSTRUCTS"), (post, "CALLS")):
        rels = _rels(sabotaged, attempt, target) | {relation}
        sabotaged.add_edge(attempt, target, rels=rels)
    m.add_transitive_calls(sabotaged)
    embeddings = [{k: sabotaged.nodes[v]["qualified_name"] for k, v in e.items()}
                  for e in V1_1.matches(sabotaged)]  # fmt: skip
    cores = {(e["R"], e["Enc"], e["S"]) for e in embeddings}
    assert cores == {("retry_with_backoff", "run_reconcile", "ReconcileSession")}
    assert "post_journal_entry" in {e["T"] for e in embeddings}
