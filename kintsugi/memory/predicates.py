"""Motif predicates on real code, and a host's code graph (spec §4.4.3; plan §2c).

``kintsugi.memory.motif`` matches structure; this module decides what the structure's nodes
*are*, on the host's own source:

- ``is_retry_wrapper``: the name and decorator heuristics nominate a function; its source
  confirms it (a loop whose ``try`` body calls one of the function's parameters, i.e. it
  re-runs a callable it was given). A named-like-retry function that does not loop is not one.
- ``has_mutable_accumulator``: the deterministic ``ast`` scan of the class; an inconclusive scan
  goes to the classifier (``claude-haiku-5-5`` through the Immunologist), whose verdict is cached
  by (symbol, source SHA-256) so the same class is never asked twice.
- ``is_side_effecting``: the host's tool registry (``side_effect: true``) reached by ``CALLS*``.

A host's graph comes from **one** ``entire graph snapshot`` of its repository, restricted to the
host's package files (the monorepo holds every host, and fixtures that must never match), with
the side-effecting tools of its ``host.yaml`` marked and function and class sources attached.
"""

from __future__ import annotations

import ast
import hashlib
import json
import textwrap
import threading
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import networkx as nx

from kintsugi.memory import motif
from kintsugi.memory.graph import Snapshot
from kintsugi.memory.motif import Predicate, Verdict

# (predicate, qualified name, source) → a verdict, or None when no classifier can decide.
Classifier = Callable[[str, str, str], bool | None]
_LOOPS = (ast.For, ast.While, ast.AsyncFor)


# ---- retry wrappers -------------------------------------------------------------------------


def scan_retry_wrapper(source: str, name: str) -> Verdict:
    """Does function ``name`` re-run a callable parameter inside a loop with a ``try``?"""
    try:
        tree = ast.parse(textwrap.dedent(source))
    except SyntaxError:
        return Verdict.INCONCLUSIVE
    fn = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == name
        ),
        None,
    )
    if fn is None:
        return Verdict.INCONCLUSIVE
    params = {a.arg for a in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)}
    if fn.args.vararg:
        params.add(fn.args.vararg.arg)
    for loop in (n for n in ast.walk(fn) if isinstance(n, _LOOPS)):
        for handler in (n for n in ast.walk(loop) if isinstance(n, ast.Try)):
            for call in (n for stmt in handler.body for n in ast.walk(stmt)):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id in params
                ):
                    return Verdict.TRUE
    return Verdict.FALSE


# ---- the classifier cache -------------------------------------------------------------------


class VerdictCache:
    """Classifier verdicts keyed by (predicate, symbol, source SHA-256), persisted as JSON."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.data: dict[str, bool] = {}
        if path is not None and path.is_file():
            self.data = {k: bool(v) for k, v in json.loads(path.read_text("utf-8")).items()}

    @staticmethod
    def key(predicate: str, name: str, source: str) -> str:
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
        return f"{predicate}|{name}|{digest}"

    def get(self, predicate: str, name: str, source: str) -> bool | None:
        return self.data.get(self.key(predicate, name, source))

    def put(self, predicate: str, name: str, source: str, verdict: bool) -> None:
        with self._lock:
            self.data[self.key(predicate, name, source)] = verdict
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                text = json.dumps(self.data, indent=1, sort_keys=True) + "\n"
                self.path.write_text(text, encoding="utf-8", newline="\n")


def registry(
    *, classify: Classifier | None = None, cache: VerdictCache | None = None
) -> dict[str, Predicate]:
    """The predicates a motif may name, confirmed on source where the graph carries it."""
    verdicts = cache or VerdictCache()

    def decide(predicate: str, graph: nx.DiGraph, node: str, scan: Verdict) -> bool:
        if scan is not Verdict.INCONCLUSIVE:
            return scan is Verdict.TRUE
        data = graph.nodes[node]
        source = data.get("source")
        if not isinstance(source, str) or classify is None:
            return False
        name = str(data.get("qualified_name") or data.get("name"))
        cached = verdicts.get(predicate, name, source)
        if cached is not None:
            return cached
        answer = classify(predicate, name, source)
        if answer is None:
            return False
        verdicts.put(predicate, name, source, answer)
        return answer

    def retry_wrapper(graph: nx.DiGraph, node: str) -> bool:
        if not motif.is_retry_wrapper(graph, node):
            return False
        source = graph.nodes[node].get("source")
        if not isinstance(source, str):
            return True  # no source in the graph (fixtures): the heuristics decide
        name = str(graph.nodes[node].get("name", ""))
        return decide("is_retry_wrapper", graph, node, scan_retry_wrapper(source, name))

    def accumulator(graph: nx.DiGraph, node: str) -> bool:
        data = graph.nodes[node]
        recorded = data.get("mutable_accumulator")
        if isinstance(recorded, bool):
            return recorded
        source = data.get("source")
        if not isinstance(source, str):
            return False
        scan = motif.scan_mutable_accumulator(source, str(data.get("name", "")))
        return decide("has_mutable_accumulator", graph, node, scan)

    return {
        "is_retry_wrapper": retry_wrapper,
        "has_mutable_accumulator": accumulator,
        "is_side_effecting": motif.is_side_effecting,
    }


# ---- a host's graph -------------------------------------------------------------------------


def restrict(snapshot: Snapshot, prefixes: Iterable[str]) -> Snapshot:
    """The part of a repository snapshot under ``prefixes`` (POSIX paths, relative)."""
    roots = tuple(p.rstrip("/") + "/" if p not in ("", ".") else "" for p in prefixes)
    keep = {s["id"] for s in snapshot.symbols if str(s["file_path"]).startswith(roots)}
    return Snapshot(
        snapshot.header,
        [f for f in snapshot.files if str(f.get("path", "")).startswith(roots)],
        snapshot.externals,
        [s for s in snapshot.symbols if s["id"] in keep],
        [r for r in snapshot.relations if r["from_id"] in keep and r["to_id"] in keep],
        snapshot.summary,
    )


def host_graph(
    snapshot: Snapshot,
    *,
    package_dir: str,
    side_effect_tools: Iterable[str],
    source_root: Path,
) -> nx.DiGraph:
    """The motif engine's graph of one host: its package only, tools marked, sources attached.

    ``side_effect_tools`` are tool names from the host's registry; a function of the package
    with that name is the tool (hosts define each tool as a function of the same name)."""
    part = restrict(snapshot, [package_dir])
    writers = set(side_effect_tools)
    tools = {s["qualified_name"] for s in part.symbols if s["name"] in writers}
    cache: dict[str, list[str]] = {}

    def attrs(record: Mapping[str, Any]) -> Mapping[str, Any]:
        if record["kind"] not in ("function", "method", "class"):
            return {}
        path = source_root / str(record["file_path"])
        if not path.is_file():
            return {}
        lines = cache.setdefault(str(path), path.read_text(encoding="utf-8").splitlines())
        start, end = int(record["start_line"]), int(record["end_line"])
        return {"source": textwrap.dedent("\n".join(lines[start - 1 : end]))}

    return part.to_code_graph(attrs=attrs, side_effect_tools=tools)


def names(graph: nx.DiGraph, embedding: Mapping[str, str]) -> dict[str, str]:
    """An embedding as {motif node: qualified name}."""
    return {k: str(graph.nodes[v].get("qualified_name") or v) for k, v in embedding.items()}
