"""Incident state machine (spec §3.4, §5.4) and the proof that every incident terminates (I3).

Every incident ends in RESOLVED, ESCALATED or PARKED. To make that checkable, the environment's
possible outcomes in each state are modelled explicitly (:data:`OUTCOMES`), and every outcome
must map to a transition. The spec's §5.4 table leaves four outcomes without an exit:

- REPRODUCED, attribution finds no suspect symbol;
- ATTRIBUTED, the budget is zero (an economic escalation, L5);
- SHIPPED, the merge is blocked or G7 fails on the merge commit;
- CANARY, the SPRT rejects but the revert fails or the re-entry limit is reached.

Each would leave an incident stuck in a non-terminal state, violating I3. The four closure
transitions below fill them (docs/EXECUTION_PLAN.md, Amendments log). Repair rounds and canary
re-entries are bounded, which makes the reachable state space a finite DAG whose every sink is
terminal: :func:`check_termination` verifies that exhaustively for given bounds.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

import networkx as nx


class IncidentState(StrEnum):
    OPEN = "OPEN"
    REPRODUCED = "REPRODUCED"
    ATTRIBUTED = "ATTRIBUTED"
    REPAIRING = "REPAIRING"
    SHIPPED = "SHIPPED"
    CANARY = "CANARY"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"
    PARKED = "PARKED"


S = IncidentState
TERMINAL: frozenset[IncidentState] = frozenset({S.RESOLVED, S.ESCALATED, S.PARKED})


@dataclass(frozen=True)
class Transition:
    source: IncidentState
    target: IncidentState
    outcome: str
    in_spec_table: bool


TRANSITIONS: tuple[Transition, ...] = (
    # Spec §5.4.
    Transition(S.OPEN, S.REPRODUCED, "g0_fails_3_of_3", True),
    Transition(S.OPEN, S.PARKED, "g0_not_reproducible_after_3_attempts", True),
    Transition(S.REPRODUCED, S.ATTRIBUTED, "cause_report_has_suspect", True),
    Transition(S.ATTRIBUTED, S.REPAIRING, "budget_positive", True),
    Transition(S.REPAIRING, S.REPAIRING, "round_failed_rounds_and_budget_remain", True),
    Transition(S.REPAIRING, S.SHIPPED, "winner_passed_g0_g6_pr_open_g7_green", True),
    Transition(S.REPAIRING, S.ESCALATED, "rounds_or_budget_exhausted_or_cheaper_treatment", True),
    Transition(S.SHIPPED, S.CANARY, "merged_or_pr_opened", True),
    Transition(S.CANARY, S.RESOLVED, "sprt_accepts_h0_or_pr_only_canary_skipped", True),
    Transition(S.CANARY, S.REPAIRING, "sprt_rejects_reverted_and_quarantined", True),
    # I3 closure (amendments).
    Transition(S.REPRODUCED, S.ESCALATED, "attribution_found_no_suspect", False),
    Transition(S.ATTRIBUTED, S.ESCALATED, "budget_zero", False),
    Transition(S.SHIPPED, S.ESCALATED, "merge_blocked", False),
    Transition(S.CANARY, S.ESCALATED, "sprt_rejects_revert_failed_or_reentry_limit", False),
)

# What the environment can do in each non-terminal state (mutually exhaustive outcomes).
OUTCOMES: Mapping[IncidentState, frozenset[str]] = {
    S.OPEN: frozenset({"g0_fails_3_of_3", "g0_not_reproducible_after_3_attempts"}),
    S.REPRODUCED: frozenset({"cause_report_has_suspect", "attribution_found_no_suspect"}),
    S.ATTRIBUTED: frozenset({"budget_positive", "budget_zero"}),
    S.REPAIRING: frozenset(
        {
            "round_failed_rounds_and_budget_remain",
            "winner_passed_g0_g6_pr_open_g7_green",
            "rounds_or_budget_exhausted_or_cheaper_treatment",
        }
    ),
    S.SHIPPED: frozenset({"merged_or_pr_opened", "merge_blocked"}),
    S.CANARY: frozenset(
        {
            "sprt_accepts_h0_or_pr_only_canary_skipped",
            "sprt_rejects_reverted_and_quarantined",
            "sprt_rejects_revert_failed_or_reentry_limit",
        }
    ),
}

_ROUND_RETRY = "round_failed_rounds_and_budget_remain"
_REENTRY = "sprt_rejects_reverted_and_quarantined"
_REENTRY_EXHAUSTED = "sprt_rejects_revert_failed_or_reentry_limit"


class TransitionError(ValueError):
    pass


def table(*, spec_only: bool = False) -> dict[tuple[IncidentState, str], IncidentState]:
    return {
        (t.source, t.outcome): t.target for t in TRANSITIONS if t.in_spec_table or not spec_only
    }


def next_state(state: IncidentState, outcome: str) -> IncidentState:
    try:
        return table()[(state, outcome)]
    except KeyError:
        raise TransitionError(f"{state} has no transition for outcome {outcome!r}") from None


def fold(events: Iterable[tuple[IncidentState, IncidentState, str]]) -> IncidentState:
    """Replay an append-only transition log (the SQLite-WAL event source) to the current state."""
    current = S.OPEN
    for source, target, outcome in events:
        if source is not current:
            raise TransitionError(f"log says {source} → {target}, but the incident is {current}")
        if next_state(source, outcome) is not target:
            raise TransitionError(f"{source} --{outcome}--> {target} is not a legal transition")
        current = target
    return current


def missing_exits(*, spec_only: bool = False) -> list[tuple[IncidentState, str]]:
    """Outcomes with no transition: each one is a way for an incident to get stuck (I3)."""
    known = table(spec_only=spec_only)
    return sorted(
        (state, outcome)
        for state, outcomes in OUTCOMES.items()
        for outcome in outcomes
        if (state, outcome) not in known
    )


# ---- bounded model: every path terminates ---------------------------------------------------


@dataclass(frozen=True)
class Bounds:
    max_rounds: int = 3
    max_canary_reentries: int = 2

    def __post_init__(self) -> None:
        if self.max_rounds < 1 or self.max_canary_reentries < 0:
            raise ValueError("need max_rounds ≥ 1 and max_canary_reentries ≥ 0")


ModelState = tuple[IncidentState, int, int]  # (state, rounds used in this attempt, re-entries)


def available_outcomes(model_state: ModelState, bounds: Bounds) -> frozenset[str]:
    state, rounds, reentries = model_state
    outcomes = set(OUTCOMES.get(state, frozenset()))
    if state is S.REPAIRING and rounds + 1 >= bounds.max_rounds:
        outcomes.discard(_ROUND_RETRY)  # no rounds left
    if state is S.CANARY:
        if reentries >= bounds.max_canary_reentries:
            outcomes.discard(_REENTRY)
        else:
            outcomes.discard(_REENTRY_EXHAUSTED)  # a revert that succeeds re-enters instead
    return frozenset(outcomes)


def step(model_state: ModelState, outcome: str, *, spec_only: bool = False) -> ModelState | None:
    state, rounds, reentries = model_state
    target = table(spec_only=spec_only).get((state, outcome))
    if target is None:
        return None
    if outcome == _ROUND_RETRY:
        return (target, rounds + 1, reentries)
    if outcome == _REENTRY:
        return (target, 0, reentries + 1)  # re-enter with new evidence: fresh rounds
    return (target, rounds, reentries)


@dataclass(frozen=True)
class TerminationReport:
    states: int
    acyclic: bool
    stuck: tuple[ModelState, ...]  # non-terminal states with an outcome that has no exit
    longest_path: int

    @property
    def every_path_terminates(self) -> bool:
        return self.acyclic and not self.stuck


def model_graph(bounds: Bounds, *, spec_only: bool = False) -> nx.DiGraph:
    start: ModelState = (S.OPEN, 0, 0)
    graph = nx.DiGraph()
    graph.add_node(start)
    frontier: list[ModelState] = [start]
    while frontier:
        current = frontier.pop()
        for outcome in sorted(available_outcomes(current, bounds)):
            successor = step(current, outcome, spec_only=spec_only)
            if successor is None:
                graph.nodes[current]["stuck"] = True
                continue
            if successor not in graph:
                graph.add_node(successor)
                frontier.append(successor)
            graph.add_edge(current, successor, outcome=outcome)
    return graph


def check_termination(bounds: Bounds, *, spec_only: bool = False) -> TerminationReport:
    graph = model_graph(bounds, spec_only=spec_only)
    stuck = tuple(sorted(n for n, data in graph.nodes(data=True) if data.get("stuck")))
    acyclic = nx.is_directed_acyclic_graph(graph)
    sinks_terminal = all(node[0] in TERMINAL for node in graph if graph.out_degree(node) == 0)
    longest = nx.dag_longest_path_length(graph) if acyclic else -1
    if not sinks_terminal:
        dead = tuple(n for n in graph if graph.out_degree(n) == 0 and n[0] not in TERMINAL)
        stuck = tuple(sorted(set(stuck) | set(dead)))
    return TerminationReport(
        states=graph.number_of_nodes(), acyclic=acyclic, stuck=stuck, longest_path=longest
    )
