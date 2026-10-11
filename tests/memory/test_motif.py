"""Motif engine on fleet-shaped fixture graphs: the full L3 learning story of spec §6.5–§6.7."""

from __future__ import annotations

from typing import Any

import networkx as nx
import pytest

from kintsugi.memory import motif as m
from kintsugi.memory.antibody import MotifSpec


def _host(
    *,
    retried_scope_constructs: bool,
    scope_side_effect: bool,
    accumulator_cls: str = "ReconcileSession",
    hoisted: bool = False,
    dedupe_guard: bool = False,
) -> nx.DiGraph:
    """A host shaped like Ledger: run_reconcile → retry_with_backoff(attempt) with a session."""
    symbols = [
        m.Symbol("run", "function", "run_reconcile"),
        m.Symbol("retry", "function", "retry_with_backoff"),
        m.Symbol("attempt", "function", "attempt"),
        m.Symbol("session", "class", accumulator_cls, attrs={"mutable_accumulator": True}),
        m.Symbol("helper", "function", "_post_lines"),
        m.Symbol("tool", "function", "post_or_fetch", attrs={"side_effect": scope_side_effect}),
        m.Symbol("fx", "function", "fx_rate"),
        m.Symbol("guard", "function", "dedupe_guard", attrs={"dedupe_guard": True}),
    ]
    relations = [
        m.Relation("run", "retry", "CALLS"),
        m.Relation("run", "attempt", "CONTAINS"),
        m.Relation("attempt", "helper", "CALLS"),
        m.Relation("helper", "tool", "CALLS"),
        m.Relation("attempt", "fx", "CALLS"),
        m.Relation("attempt", "requests.post", "CALLS"),  # external: skipped and counted
    ]
    if retried_scope_constructs:
        relations.append(m.Relation("attempt", "session", "CONSTRUCTS"))
    if hoisted:
        relations.append(m.Relation("run", "session", "CONSTRUCTS"))
    if dedupe_guard:
        relations.append(m.Relation("attempt", "guard", "CALLS"))
    graph = m.build_graph(symbols, relations)
    m.add_transitive_calls(graph)
    return graph


LEDGER_BEFORE = _host(retried_scope_constructs=True, scope_side_effect=True)
LEDGER_AFTER = _host(retried_scope_constructs=False, scope_side_effect=True, hoisted=True)
SCOUT = _host(
    retried_scope_constructs=True, scope_side_effect=False, accumulator_cls="ScratchBuffer"
)
CLINIC_A = _host(retried_scope_constructs=True, scope_side_effect=True)
OSS_1 = _host(retried_scope_constructs=True, scope_side_effect=True)
FLEET = {"scout": SCOUT, "clinic-a": CLINIC_A, "oss-1": OSS_1}
VULNERABLE = {"clinic-a", "oss-1"}

_CORE: dict[str, Any] = {
    "nodes": {
        "R": {"kind": "function", "pred": ["is_retry_wrapper"]},
        "Enc": {"kind": "function"},
        "F": {"kind": "function"},
        "S": {"kind": "class", "pred": ["has_mutable_accumulator"]},
    },
    "edges": [["Enc", "R", "CALLS"], ["Enc", "F", "CONTAINS"], ["F", "S", "CONSTRUCTS"]],
}
MOTIF_V1_0 = MotifSpec.model_validate(_CORE)
MOTIF_V1_1 = MotifSpec.model_validate(
    {
        "nodes": {**_CORE["nodes"], "T": {"kind": "function", "pred": ["is_side_effecting"]}},
        "edges": [*_CORE["edges"], ["F", "T", "CALLS*"]],
    }
)


def test_external_relations_are_skipped_and_counted() -> None:
    assert LEDGER_BEFORE.graph["skipped_relations"] == 1


def test_transitive_calls_reach_through_helpers() -> None:
    assert "CALLS*" in LEDGER_BEFORE["attempt"]["tool"]["rels"]
    assert "CALLS*" in LEDGER_BEFORE["attempt"]["helper"]["rels"]
    assert "CALLS" in LEDGER_BEFORE["attempt"]["helper"]["rels"]  # the direct edge is kept


def test_depth_limited_transitive_calls() -> None:
    graph = _host(retried_scope_constructs=True, scope_side_effect=True)
    shallow = m.build_graph(
        [m.Symbol(n, "function", n) for n in ("a", "b", "c")],
        [m.Relation("a", "b", "CALLS"), m.Relation("b", "c", "CALLS")],
    )
    m.add_transitive_calls(shallow, max_depth=1)
    assert not shallow.has_edge("a", "c")
    assert graph.has_edge("attempt", "tool")


def test_v1_0_matches_the_origin_before_but_not_after_the_fix() -> None:
    matcher = m.MotifMatcher(MOTIF_V1_0)
    [embedding] = matcher.matches(LEDGER_BEFORE)
    assert embedding == {"R": "retry", "Enc": "run", "F": "attempt", "S": "session"}
    assert not matcher.matches_any(LEDGER_AFTER)


def test_v1_0_over_matches_scout_so_it_is_not_admissible() -> None:
    # The disclosed look-alike (spec §5.9.2): per-attempt reset is intended in Scout.
    result = m.validate(
        m.MotifMatcher(MOTIF_V1_0), before_fix=LEDGER_BEFORE, after_fix=LEDGER_AFTER,
        fleet=FLEET, vulnerable_hosts=VULNERABLE,
    )  # fmt: skip
    assert result.false_positive_hosts == ("scout",)
    assert result.precision == pytest.approx(2 / 3)
    assert not result.admissible


def test_v1_1_learned_precondition_excludes_scout() -> None:
    # Spec §6.7 step 19: "F transitively CALLS a side-effecting tool inside the retried scope".
    matcher = m.MotifMatcher(MOTIF_V1_1)
    result = m.validate(
        matcher, before_fix=LEDGER_BEFORE, after_fix=LEDGER_AFTER,
        fleet=FLEET, vulnerable_hosts=VULNERABLE,
    )  # fmt: skip
    assert result.matches_before_fix
    assert not result.matches_after_fix
    assert result.true_positive_hosts == ("clinic-a", "oss-1")
    assert result.false_positive_hosts == ()
    assert result.precision == 1.0
    assert result.admissible
    assert not matcher.matches_any(SCOUT)


def test_negative_patterns_tolerate_guarded_hosts() -> None:
    tolerant = MotifSpec.model_validate(
        {**_CORE, "negative": [{"nodes": {"G": {"kind": "function", "pred": ["is_dedupe_guard"]}},
                                "edges": [["F", "G", "CALLS"]]}]}
    )  # fmt: skip

    def is_dedupe_guard(graph: nx.DiGraph, node: str) -> bool:
        return graph.nodes[node].get("dedupe_guard") is True

    matcher = m.MotifMatcher(tolerant, predicates={"is_dedupe_guard": is_dedupe_guard})
    guarded = _host(retried_scope_constructs=True, scope_side_effect=True, dedupe_guard=True)
    assert matcher.matches_any(LEDGER_BEFORE)
    assert not matcher.matches_any(guarded)


def test_python_has_no_field_write_relations() -> None:
    bad = MotifSpec.model_validate(
        {
            "nodes": {"F": {"kind": "function"}, "S": {"kind": "class"}},
            "edges": [["F", "S", "WRITES_FIELD"]],
        }
    )
    with pytest.raises(ValueError, match="does not provide"):
        m.MotifMatcher(bad)


def test_relations_are_feature_detected_from_capabilities() -> None:
    capabilities = {
        "supported_relation_types": ["CALLS", "CONSTRUCTS", "HTTP_CALLS", "FILE_CHANGES_WITH"],
        "heuristic_relation_types": ["HTTP_CALLS"],
        "relation_support_by_language": {"Python": ["CALLS", "CONSTRUCTS"]},
    }
    allowed = m.allowed_relations(capabilities)
    assert allowed == {"CALLS", "CONSTRUCTS", "HTTP_CALLS", "FILE_CHANGES_WITH", "CALLS*"}
    with pytest.raises(ValueError, match="no relation support"):
        m.allowed_relations(capabilities, language="Cobol")


def test_unknown_predicates_are_refused() -> None:
    node = {"kind": "function", "pred": ["nope"]}
    spec = MotifSpec.model_validate({"nodes": {"F": node}, "edges": []})
    with pytest.raises(ValueError, match="unknown predicates"):
        m.MotifMatcher(spec)


@pytest.mark.parametrize(
    ("name", "decorators", "expected"),
    [
        ("retry_with_backoff", (), True),
        ("with_retries", (), True),
        ("call_backoff", (), True),
        ("fetch", ("tenacity.retry",), True),
        ("entry_point", (), False),
        ("retrieve_docs", (), False),
    ],
)
def test_retry_wrapper_heuristics(name: str, decorators: tuple[str, ...], expected: bool) -> None:
    graph = m.build_graph([m.Symbol("x", "function", name, attrs={"decorators": decorators})], [])
    assert m.is_retry_wrapper(graph, "x") is expected


# ---- deterministic mutable-accumulator scan (stdlib ast) ------------------------------------

LEDGER_SESSION = """
class ReconcileSession:
    def __init__(self, batch):
        self.batch = batch
        self.posted = set()

    def post(self, line):
        if line.key in self.posted:
            return
        self.posted.add(line.key)
"""

DATACLASS_BUFFER = """
from dataclasses import dataclass, field

@dataclass
class ScratchBuffer:
    hits: list = field(default_factory=list)

    def record(self, hit):
        self.hits += [hit]
"""

IMMUTABLE = """
class Money:
    def __init__(self, cents):
        self.cents = cents

    def plus(self, other):
        return Money(self.cents + other.cents)
"""

UNMUTATED = """
class Snapshot:
    def __init__(self, rows):
        self.rows = list(rows)

    def first(self):
        return self.rows[0]
"""


@pytest.mark.parametrize(
    ("source", "cls", "verdict"),
    [
        (LEDGER_SESSION, "ReconcileSession", m.Verdict.TRUE),
        (DATACLASS_BUFFER, "ScratchBuffer", m.Verdict.TRUE),
        (IMMUTABLE, "Money", m.Verdict.FALSE),
        (UNMUTATED, "Snapshot", m.Verdict.INCONCLUSIVE),
        (LEDGER_SESSION, "Missing", m.Verdict.INCONCLUSIVE),
        ("class (:", "X", m.Verdict.INCONCLUSIVE),
    ],
)
def test_scan_mutable_accumulator(source: str, cls: str, verdict: m.Verdict) -> None:
    assert m.scan_mutable_accumulator(source, cls) is verdict


def test_has_mutable_accumulator_uses_source_when_no_verdict_is_recorded() -> None:
    graph = m.build_graph(
        [
            m.Symbol("s", "class", "ReconcileSession", attrs={"source": LEDGER_SESSION}),
            m.Symbol("i", "class", "Money", attrs={"source": IMMUTABLE}),
        ],
        [],
    )
    assert m.has_mutable_accumulator(graph, "s")
    assert not m.has_mutable_accumulator(graph, "i")


def test_http_writes_count_as_side_effects() -> None:
    graph = m.build_graph(
        [
            m.Symbol("f", "function", "sync"),
            m.Symbol("h", "function", "crm_client.create", attrs={"http_method": "post"}),
        ],
        [m.Relation("f", "h", "CALLS")],
    )
    m.add_transitive_calls(graph)
    assert m.is_side_effecting(graph, "f")
