"""Contrastive precondition learning: immune tolerance from a false match (spec §6.7 step 19).

After a quarantine, the antibody's motif matched a host it should not have (the negative) as
well as the hosts it protects (the positives). For each match the learner looks at the motif's
embedding and its 2-hop neighbourhood and evaluates a fixed catalogue of candidate predicates:

==========================  ==============================================================
``side_effect_in_scope``    the retried scope F transitively calls a side-effecting tool
``scope_returns_state``     F is declared to return the accumulator S
``accumulator_has_cursor``  S keeps an integer resume cursor
``enclosing_takes_state``   the enclosing function accepts S as a parameter (resumable)
==========================  ==============================================================

A candidate is *admissible* when it holds on every positive (it may not cost any protection);
it *covers* the negatives on which it is false. Greedy set cover picks admissible candidates
covering the most uncovered negatives (ties in catalogue order) until every negative is
excluded. Each chosen candidate carries its motif refinement (nodes, edges or a predicate), so
the result is the next motif version (a minor bump) plus its human-readable preconditions. The
Immunologist (Opus) writes the rationale; the Auditor's check is :func:`verify`: the refined
motif, re-matched on the same graphs, must keep every positive and exclude every negative.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import networkx as nx

from kintsugi.memory import motif as m
from kintsugi.memory.antibody import MotifNode, MotifSpec

Holds = Callable[[nx.DiGraph, Mapping[str, str]], bool]


@dataclass(frozen=True)
class Example:
    host: str
    graph: nx.DiGraph
    embedding: Mapping[str, str]  # motif node → graph node (the current version's match)


@dataclass(frozen=True)
class Candidate:
    name: str
    precondition: str
    holds: Holds
    nodes: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)  # new motif nodes
    edges: tuple[tuple[str, str, str], ...] = ()  # new motif edges
    node_preds: Mapping[str, tuple[str, ...]] = field(default_factory=dict)  # extra predicates


def _rels(graph: nx.DiGraph, u: str, v: str) -> set[str]:
    return set(graph[u][v]["rels"]) if graph.has_edge(u, v) else set()


def _side_effect_in_scope(graph: nx.DiGraph, e: Mapping[str, str]) -> bool:
    scope = e["F"]
    return any(
        m.TRANSITIVE_CALLS in data["rels"] and m.is_side_effecting(graph, target)
        for _, target, data in graph.out_edges(scope, data=True)
    )


def _scope_returns_state(graph: nx.DiGraph, e: Mapping[str, str]) -> bool:
    return "RETURNS_TYPE" in _rels(graph, e["F"], e["S"])


def _enclosing_takes_state(graph: nx.DiGraph, e: Mapping[str, str]) -> bool:
    return "PARAM_TYPE" in _rels(graph, e["Enc"], e["S"])


def _accumulator_has_cursor(graph: nx.DiGraph, e: Mapping[str, str]) -> bool:
    from kintsugi.memory.predicates import has_resume_cursor

    return has_resume_cursor(graph, e["S"])


CANDIDATES: tuple[Candidate, ...] = (
    Candidate(
        "side_effect_in_scope",
        "a side-effecting callee inside the retried scope",
        _side_effect_in_scope,
        nodes={"T": {"kind": "function", "pred": ["is_side_effecting"]}},
        edges=(("F", "T", m.TRANSITIVE_CALLS),),
    ),
    Candidate(
        "scope_returns_state",
        "the retried scope returns its accumulator",
        _scope_returns_state,
        edges=(("F", "S", "RETURNS_TYPE"),),
    ),
    Candidate(
        "accumulator_has_cursor",
        "the accumulator keeps a resume cursor",
        _accumulator_has_cursor,
        node_preds={"S": ("has_resume_cursor",)},
    ),
    Candidate(
        "enclosing_takes_state",
        "the enclosing function accepts the accumulator (resumable across calls)",
        _enclosing_takes_state,
        edges=(("Enc", "S", "PARAM_TYPE"),),
    ),
)


def neighbourhood(graph: nx.DiGraph, embedding: Mapping[str, str], hops: int = 2) -> set[str]:
    """The nodes within ``hops`` of the embedding, in either direction (the evidence window)."""
    undirected = graph.to_undirected(as_view=True)
    found: set[str] = set()
    for node in embedding.values():
        found |= set(nx.single_source_shortest_path_length(undirected, node, cutoff=hops))
    return found


@dataclass(frozen=True)
class Learned:
    selected: tuple[str, ...]
    table: dict[str, dict[str, bool]]  # candidate → host → holds
    admissible: tuple[str, ...]
    motif: MotifSpec
    preconditions: tuple[str, ...]
    separated: bool
    neighbourhoods: dict[str, int]  # host → size of its 2-hop window

    def as_dict(self) -> dict[str, Any]:
        return {
            "selected": list(self.selected),
            "admissible": list(self.admissible),
            "table": self.table,
            "preconditions": list(self.preconditions),
            "separated": self.separated,
            "neighbourhoods": self.neighbourhoods,
            "motif": self.motif.model_dump(mode="json"),
        }


def refine(motif: MotifSpec, chosen: Sequence[Candidate]) -> MotifSpec:
    nodes = {name: node.model_dump() for name, node in motif.nodes.items()}
    edges = [tuple(e) for e in motif.edges]
    for candidate in chosen:
        for name, node in candidate.nodes.items():
            nodes.setdefault(name, dict(node))
        edges += [e for e in candidate.edges if e not in edges]
        for name, preds in candidate.node_preds.items():
            current = nodes[name]["pred"]
            nodes[name]["pred"] = [*current, *(p for p in preds if p not in current)]
    return MotifSpec(
        nodes={k: MotifNode.model_validate(v) for k, v in nodes.items()},
        edges=[(str(a), str(b), str(c)) for a, b, c in edges],
        negative=list(motif.negative),
    )


def learn(
    motif: MotifSpec,
    positives: Sequence[Example],
    negatives: Sequence[Example],
    *,
    candidates: Sequence[Candidate] = CANDIDATES,
) -> Learned:
    if not positives or not negatives:
        raise ValueError("contrastive learning needs at least one positive and one negative")
    table = {
        c.name: {ex.host: bool(c.holds(ex.graph, ex.embedding)) for ex in (*positives, *negatives)}
        for c in candidates
    }
    admissible = [c for c in candidates if all(table[c.name][p.host] for p in positives)]
    uncovered = {n.host for n in negatives}
    chosen: list[Candidate] = []
    while uncovered:
        best, gain = None, 0
        for candidate in admissible:
            if candidate in chosen:
                continue
            covers = {h for h in uncovered if not table[candidate.name][h]}
            if len(covers) > gain:
                best, gain = candidate, len(covers)
        if best is None:
            break
        chosen.append(best)
        uncovered -= {h for h in uncovered if not table[best.name][h]}
    return Learned(
        selected=tuple(c.name for c in chosen),
        table=table,
        admissible=tuple(c.name for c in admissible),
        motif=refine(motif, chosen),
        preconditions=tuple(c.precondition for c in chosen),
        separated=not uncovered,
        neighbourhoods={
            ex.host: len(neighbourhood(ex.graph, ex.embedding)) for ex in (*positives, *negatives)
        },
    )


def verify(
    learned: Learned,
    positives: Sequence[Example],
    negatives: Sequence[Example],
    *,
    predicates: Mapping[str, m.Predicate] | None = None,
) -> list[str]:
    """The Auditor's check: the refined motif keeps every positive and excludes every negative."""
    matcher = m.MotifMatcher(learned.motif, predicates=predicates)
    problems = [f"{p.host}: the refined motif no longer matches a positive"
                for p in positives if not matcher.matches_any(p.graph)]  # fmt: skip
    problems += [f"{n.host}: the refined motif still matches a negative"
                 for n in negatives if matcher.matches_any(n.graph)]  # fmt: skip
    return problems
