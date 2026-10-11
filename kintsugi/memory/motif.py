"""Structural motif engine (spec §4.4.3; docs/EXECUTION_PLAN.md §2c).

Code graphs come from entire-graph snapshots: symbols become nodes, relations become directed
edges that carry the *set* of relation types between two symbols. A motif is matched with VF2
subgraph **monomorphism**: extra edges in the host graph are allowed.

Relations are feature-detected. A motif may only use relations that entire-graph supports for
the host's language (``entire graph capabilities --json``) plus ``CALLS*``, the transitive
closure of ``CALLS``/``ASYNC_CALLS`` computed here before matching (VF2 matches edges exactly).
For Python, entire-graph v0.4.0 has ``CONSTRUCTS`` but no ``READS_FIELD``/``WRITES_FIELD``/
``ACCESSES``/``IMPLEMENTS``, so a constructor call is ``[F, S, CONSTRUCTS]``, and a mutable
accumulator is judged from the class source with the stdlib ``ast`` (Haiku only decides
inconclusive cases, in Stage 7).

Validation (§4.4.3): an antibody's motif must match the origin host before the fix, must not
match it after the fix, and must reach precision ≥ 0.9 on the fleet, where a match on a host
known to be healthy for the pathogen is a false positive.
"""

from __future__ import annotations

import ast
import re
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import networkx as nx
from networkx.algorithms.isomorphism import DiGraphMatcher

from kintsugi.memory.antibody import MotifNode, MotifPattern, MotifSpec

# entire-graph v0.4.0 `capabilities --json`, verified 2026-10-11.
PYTHON_RELATIONS: frozenset[str] = frozenset(
    {
        "ASYNC_CALLS", "CALLS", "CONSTRUCTS", "CONTAINS", "DATA_FLOWS", "DEFINES", "EXTENDS",
        "HANDLES_GRAPHQL", "IMPORTS", "INHERITS", "OVERRIDES", "PARAM_TYPE", "RETURNS_TYPE",
        "USES_TYPE",
    }
)  # fmt: skip
HEURISTIC_RELATIONS: frozenset[str] = frozenset(
    {"EMITS", "HANDLES_ROUTE", "HANDLES_TOOL", "HTTP_CALLS", "LISTENS_ON", "SIMILAR_TO", "TESTS"}
)
# The `full` profile's git co-change relation.
PROFILE_RELATIONS: frozenset[str] = frozenset({"FILE_CHANGES_WITH"})
TRANSITIVE_CALLS = "CALLS*"
DERIVED_RELATIONS: frozenset[str] = frozenset({TRANSITIVE_CALLS})

# Motif kinds and the snapshot kinds they accept. Pinned against real snapshot output in Stage 2
# (spike A9); unknown kinds match exactly.
KIND_ALIASES: dict[str, frozenset[str]] = {
    "function": frozenset({"function", "method", "lambda", "closure"}),
    "class": frozenset({"class"}),
}
MIN_PRECISION = 0.9

Predicate = Callable[[nx.DiGraph, str], bool]


def allowed_relations(
    capabilities: Mapping[str, Any] | None = None, language: str = "Python"
) -> frozenset[str]:
    """Relations a motif may use for ``language``, from live capabilities when given."""
    if capabilities is None:
        base = PYTHON_RELATIONS | HEURISTIC_RELATIONS | PROFILE_RELATIONS
    else:
        by_language = capabilities.get("relation_support_by_language", {})
        if language not in by_language:
            raise ValueError(f"entire-graph has no relation support for {language}")
        supported = set(capabilities.get("supported_relation_types", []))
        heuristic = set(capabilities.get("heuristic_relation_types", [])) & supported
        profile = {"FILE_CHANGES_WITH"} & supported
        base = frozenset(by_language[language]) | heuristic | profile
    return frozenset(base | DERIVED_RELATIONS)


# ---- graphs ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Symbol:
    id: str
    kind: str
    name: str
    file: str | None = None
    attrs: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Relation:
    source: str
    target: str
    relation: str


def build_graph(symbols: Iterable[Symbol], relations: Iterable[Relation]) -> nx.DiGraph:
    """A code graph whose edges carry ``rels``: the set of relation types between two symbols.

    Relations whose endpoints are not symbols of this graph (e.g. external libraries) are
    skipped and counted in ``graph.graph["skipped_relations"]``.
    """
    graph = nx.DiGraph()
    for symbol in symbols:
        graph.add_node(
            symbol.id, **dict(symbol.attrs), _id=symbol.id, kind=symbol.kind, name=symbol.name,
            file=symbol.file,
        )  # fmt: skip
    skipped = 0
    for relation in relations:
        if relation.source not in graph or relation.target not in graph:
            skipped += 1
            continue
        if graph.has_edge(relation.source, relation.target):
            graph[relation.source][relation.target]["rels"].add(relation.relation)
        else:
            graph.add_edge(relation.source, relation.target, rels={relation.relation})
    graph.graph["skipped_relations"] = skipped
    return graph


def add_transitive_calls(
    graph: nx.DiGraph,
    *,
    via: tuple[str, ...] = ("CALLS", "ASYNC_CALLS"),
    max_depth: int | None = None,
) -> None:
    """Add ``CALLS*`` from every symbol to everything it reaches through ``via`` edges."""
    calls: dict[str, list[str]] = {}
    for source, target, data in graph.edges(data=True):
        if data["rels"] & set(via):
            calls.setdefault(source, []).append(target)
    for origin in list(graph.nodes):
        seen: set[str] = set()
        queue: deque[tuple[str, int]] = deque((callee, 1) for callee in calls.get(origin, []))
        while queue:
            node, depth = queue.popleft()
            if node in seen:
                continue
            seen.add(node)
            if max_depth is None or depth < max_depth:
                queue.extend((callee, depth + 1) for callee in calls.get(node, []))
        for reached in seen - {origin}:
            if graph.has_edge(origin, reached):
                graph[origin][reached]["rels"].add(TRANSITIVE_CALLS)
            else:
                graph.add_edge(origin, reached, rels={TRANSITIVE_CALLS})


# ---- predicates -----------------------------------------------------------------------------


class Verdict(StrEnum):
    TRUE = "true"
    FALSE = "false"
    INCONCLUSIVE = "inconclusive"


_RETRY_NAME = re.compile(r"(^|_)retr(y|ies|ying)(_|$)|^with_retr|backoff", re.IGNORECASE)
_RETRY_DECORATORS = ("tenacity.retry", "retry", "backoff.on_exception", "backoff.on_predicate")
_ACCUMULATOR_FACTORIES = {"list", "dict", "set", "defaultdict", "deque", "OrderedDict", "Counter"}
_MUTATORS = {
    "append", "appendleft", "extend", "extendleft", "add", "update", "insert", "setdefault",
    "pop", "popleft", "popitem", "remove", "discard", "clear",
}  # fmt: skip
_SIDE_EFFECT_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def is_retry_wrapper(graph: nx.DiGraph, node: str) -> bool:
    """Name and decorator heuristics (spec §4.4.3); confirmed with `entire graph def` in Stage 7."""
    data = graph.nodes[node]
    if data.get("retry_wrapper") is True:
        return True
    if any(str(d) in _RETRY_DECORATORS for d in data.get("decorators", ())):
        return True
    return bool(_RETRY_NAME.search(str(data.get("name", ""))))


def is_side_effecting(graph: nx.DiGraph, node: str) -> bool:
    """The node, or anything it reaches through ``CALLS*``, is a side-effecting tool or HTTP write.

    ``side_effect: true`` comes from the host's tool registry; ``http_method`` from HTTP_CALLS.
    Requires :func:`add_transitive_calls` to have run.
    """

    def writes(candidate: str) -> bool:
        data = graph.nodes[candidate]
        return data.get("side_effect") is True or str(data.get("http_method", "")).upper() in (
            _SIDE_EFFECT_METHODS
        )

    if writes(node):
        return True
    return any(
        TRANSITIVE_CALLS in data["rels"] and writes(target)
        for _, target, data in graph.out_edges(node, data=True)
    )


def has_mutable_accumulator(graph: nx.DiGraph, node: str) -> bool:
    """A recorded verdict wins; otherwise the class source is scanned. Inconclusive is False here;
    Stage 7 sends inconclusive classes to the Haiku classifier and caches its verdict."""
    data = graph.nodes[node]
    recorded = data.get("mutable_accumulator")
    if isinstance(recorded, bool):
        return recorded
    source = data.get("source")
    if isinstance(source, str):
        return scan_mutable_accumulator(source, str(data.get("name", ""))) is Verdict.TRUE
    return False


def scan_mutable_accumulator(source: str, class_name: str) -> Verdict:
    """Deterministic AST check: does ``class_name`` hold a list/dict/set field it mutates?"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return Verdict.INCONCLUSIVE
    target = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == class_name), None
    )
    if target is None:
        return Verdict.INCONCLUSIVE
    fields = _accumulator_fields(target)
    if not fields:
        return Verdict.FALSE
    for node in ast.walk(target):
        if _mutates(node, fields):
            return Verdict.TRUE
    return Verdict.INCONCLUSIVE


def _is_factory(value: ast.expr | None) -> bool:
    if isinstance(value, ast.List | ast.Dict | ast.Set | ast.ListComp | ast.DictComp | ast.SetComp):
        return True
    if isinstance(value, ast.Call):
        func = value.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name in _ACCUMULATOR_FACTORIES:
            return True
        if name == "field":  # dataclasses.field(default_factory=list)
            return any(
                kw.arg == "default_factory"
                and isinstance(kw.value, ast.Name)
                and kw.value.id in _ACCUMULATOR_FACTORIES
                for kw in value.keywords
            )
    return False


def _self_attr(node: ast.expr) -> str | None:
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    ):
        return node.attr
    return None


def _accumulator_fields(cls: ast.ClassDef) -> set[str]:
    fields: set[str] = set()
    for stmt in cls.body:  # class-level / dataclass fields
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            if _is_factory(stmt.value):
                fields.add(stmt.target.id)
        elif isinstance(stmt, ast.Assign) and _is_factory(stmt.value):
            fields.update(t.id for t in stmt.targets if isinstance(t, ast.Name))
    for node in ast.walk(cls):  # self.x = [] anywhere in the class
        if isinstance(node, ast.Assign) and _is_factory(node.value):
            fields.update(name for t in node.targets if (name := _self_attr(t)))
        elif isinstance(node, ast.AnnAssign) and _is_factory(node.value):
            name = _self_attr(node.target)
            if name:
                fields.add(name)
    return fields


def _mutates(node: ast.AST, fields: set[str]) -> bool:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return node.func.attr in _MUTATORS and _self_attr(node.func.value) in fields
    if isinstance(node, ast.Assign | ast.AugAssign | ast.Delete):
        targets = node.targets if isinstance(node, ast.Assign | ast.Delete) else [node.target]
        for target in targets:
            if isinstance(node, ast.AugAssign) and _self_attr(target) in fields:
                return True
            if isinstance(target, ast.Subscript) and _self_attr(target.value) in fields:
                return True
    return False


BUILTIN_PREDICATES: dict[str, Predicate] = {
    "is_retry_wrapper": is_retry_wrapper,
    "is_side_effecting": is_side_effecting,
    "has_mutable_accumulator": has_mutable_accumulator,
}


# ---- matching -------------------------------------------------------------------------------


def _pattern(nodes: Mapping[str, MotifNode], edges: Iterable[tuple[str, str, str]]) -> nx.DiGraph:
    pattern = nx.DiGraph()
    for name, node in nodes.items():
        pattern.add_node(name, kind=node.kind, pred=tuple(node.pred))
    for source, target, relation in edges:
        if pattern.has_edge(source, target):
            pattern[source][target]["rels"].add(relation)
        else:
            pattern.add_edge(source, target, rels={relation})
    return pattern


class MotifMatcher:
    """Matches a motif against code graphs with a fixed predicate registry and relation set."""

    def __init__(
        self,
        motif: MotifSpec,
        *,
        predicates: Mapping[str, Predicate] | None = None,
        relations: frozenset[str] | None = None,
    ) -> None:
        allowed = allowed_relations() if relations is None else relations
        unsupported = motif.relations() - allowed
        if unsupported:
            raise ValueError(f"motif uses relations entire-graph does not provide: {unsupported}")
        self.predicates = {**BUILTIN_PREDICATES, **(predicates or {})}
        nodes = [*motif.nodes.values(), *(n for neg in motif.negative for n in neg.nodes.values())]
        missing = {p for node in nodes for p in node.pred} - set(self.predicates)
        if missing:
            raise ValueError(f"unknown predicates: {missing}")
        self.motif = motif
        self._pattern = _pattern(motif.nodes, motif.edges)

    def matches(self, graph: nx.DiGraph) -> list[dict[str, str]]:
        """Every embedding of the motif, as {motif node: graph node}, minus tolerated ones."""
        cache: dict[tuple[str, str], bool] = {}

        def holds(predicate: str, node: str) -> bool:
            key = (predicate, node)
            if key not in cache:
                cache[key] = self.predicates[predicate](graph, node)
            return cache[key]

        def node_match(g: Mapping[str, Any], p: Mapping[str, Any]) -> bool:
            if "bind" in p:
                return g["_id"] == p["bind"]
            kinds = KIND_ALIASES.get(p["kind"], frozenset({p["kind"]}))
            return g.get("kind") in kinds and all(holds(name, g["_id"]) for name in p["pred"])

        def edge_match(g: Mapping[str, Any], p: Mapping[str, Any]) -> bool:
            return p["rels"] <= g["rels"]

        found: list[dict[str, str]] = []
        matcher = DiGraphMatcher(graph, self._pattern, node_match=node_match, edge_match=edge_match)
        for embedding in matcher.subgraph_monomorphisms_iter():
            mapping = {motif_node: host_node for host_node, motif_node in embedding.items()}
            negatives = self.motif.negative
            if not any(self._tolerated(graph, mapping, neg, node_match) for neg in negatives):
                found.append(mapping)
        return sorted(found, key=lambda m: sorted(m.items()))

    def matches_any(self, graph: nx.DiGraph) -> bool:
        return bool(self.matches(graph))

    def _tolerated(
        self,
        graph: nx.DiGraph,
        mapping: Mapping[str, str],
        negative: MotifPattern,
        node_match: Callable[[Mapping[str, Any], Mapping[str, Any]], bool],
    ) -> bool:
        referenced = {n for s, t, _ in negative.edges for n in (s, t)} - set(negative.nodes)
        pattern = _pattern({**{n: self.motif.nodes[n] for n in referenced}, **negative.nodes},
                           negative.edges)  # fmt: skip
        for name in referenced:
            pattern.nodes[name]["bind"] = mapping[name]
        matcher = DiGraphMatcher(
            graph, pattern, node_match=node_match, edge_match=lambda g, p: p["rels"] <= g["rels"]
        )
        return any(True for _ in matcher.subgraph_monomorphisms_iter())


# ---- validation -----------------------------------------------------------------------------


@dataclass(frozen=True)
class MotifValidation:
    matches_before_fix: bool
    matches_after_fix: bool
    precision: float | None
    true_positive_hosts: tuple[str, ...]
    false_positive_hosts: tuple[str, ...]

    @property
    def admissible(self) -> bool:
        precise = self.precision is None or self.precision >= MIN_PRECISION
        return self.matches_before_fix and not self.matches_after_fix and precise


def validate(
    matcher: MotifMatcher,
    *,
    before_fix: nx.DiGraph,
    after_fix: nx.DiGraph,
    fleet: Mapping[str, nx.DiGraph] | None = None,
    vulnerable_hosts: Iterable[str] = (),
) -> MotifValidation:
    vulnerable = set(vulnerable_hosts)
    matched = sorted(host for host, graph in (fleet or {}).items() if matcher.matches_any(graph))
    true_pos = tuple(h for h in matched if h in vulnerable)
    false_pos = tuple(h for h in matched if h not in vulnerable)
    precision = len(true_pos) / len(matched) if matched else None
    return MotifValidation(
        matches_before_fix=matcher.matches_any(before_fix),
        matches_after_fix=matcher.matches_any(after_fix),
        precision=precision,
        true_positive_hosts=true_pos,
        false_positive_hosts=false_pos,
    )
