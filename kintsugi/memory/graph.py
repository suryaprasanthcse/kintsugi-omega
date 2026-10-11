"""entire-graph v0.4.0 adapter (spec §4.4; docs/EXECUTION_PLAN.md §2c).

Every call goes through the subprocess gateway with a timeout. JSON outputs are parsed
strictly: a missing required field raises :class:`GraphSchemaError` instead of flowing on
as ``None``.

Command mapping, verified against the installed plugin. v0.4.0 has no ``context``, ``callers``
or ``callees`` subcommands; they are provided here as:

- :func:`callers` → ``neighbors --relation CALLS --direction in``
- :func:`callees` → ``neighbors --relation CALLS --direction out``
- :func:`context` → ``search --format agent`` (budgeted context regions)

Snapshot schema (spike A9, golden files in ``tests/fixtures/graph/snapshots``): one header
record (``schema_version`` 1.1, no ``record_type``), then ``file``, ``external``, ``symbol`` and
``relation`` records, then one ``summary``. A nested closure is its own ``function`` symbol,
reached from its enclosing function by ``CONTAINS``; the enclosing function is *also* credited
with the closure's calls and constructions, so motifs must bind the retried scope through
``CONTAINS`` rather than through the enclosing function.
"""

from __future__ import annotations

import json
import os
import textwrap
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import networkx as nx

from kintsugi import PINNED_TOOLCHAIN
from kintsugi.memory.motif import Relation, Symbol, add_transitive_calls, build_graph
from kintsugi.sandbox import proc

PathLike = str | os.PathLike[str]
Profile = Literal["syntax-only", "fast", "full"]
SNAPSHOT_SCHEMA = "1.1"
DEFAULT_TIMEOUT_S = 300.0

_SYMBOL_FIELDS = ("id", "kind", "name", "qualified_name", "file_path", "start_line", "end_line")
_RELATION_FIELDS = ("from_id", "to_id", "type", "resolution", "target_kind", "confidence")
_RECORD_TYPES = ("file", "external", "symbol", "relation", "summary")


class GraphError(RuntimeError):
    pass


class GraphSchemaError(GraphError):
    """entire-graph output does not have the shape this adapter was pinned against."""


def _run(
    args: list[str],
    repo: PathLike | None,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    input_text: str | None = None,
    extra_env: Mapping[str, str] | None = None,
) -> str:
    argv = ["entire", "graph", *args]
    if repo is not None:
        argv += ["--repo", os.fspath(repo)]
    result = proc.run(argv, timeout_s=timeout_s, input_text=input_text, extra_env=extra_env)
    if not result.ok:
        detail = (result.stderr or result.stdout).strip()[-1500:]
        raise GraphError(f"{' '.join(argv[2:4])} failed ({result.returncode}): {detail}")
    return result.stdout


def _json(args: list[str], repo: PathLike | None, **kw: Any) -> Any:
    text = _run(args, repo, **kw)
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise GraphSchemaError(f"expected JSON from {args[0]}: {error}") from None


def _require(record: Mapping[str, Any], fields: Iterable[str], what: str) -> None:
    missing = [name for name in fields if name not in record]
    if missing:
        raise GraphSchemaError(f"{what} record lacks {missing}: {json.dumps(record)[:300]}")


# ---- health and setup -----------------------------------------------------------------------


def version() -> str:
    data = _json(["version", "--json"], None)
    _require(data, ("provider", "version"), "version")
    if data["provider"] != "entire-graph":
        raise GraphSchemaError(f"unexpected provider {data['provider']!r}")
    return str(data["version"])


def require_pinned() -> None:
    found, pinned = version(), PINNED_TOOLCHAIN["entire-graph"]
    if found != pinned:
        raise GraphError(f"entire-graph {found} is not the pinned {pinned}")


def doctor() -> dict[str, Any]:
    """Read-only health check; Omega requires ``no_egress`` and a writable plugin data dir."""
    data = _json(["doctor", "--json"], None)
    _require(data, ("no_egress", "plugin_data_dir", "version"), "doctor")
    return data


def doctor_ok(report: Mapping[str, Any]) -> bool:
    return report.get("no_egress") is True and report.get("plugin_data_dir") == "writable"


def capabilities() -> dict[str, Any]:
    data = _json(["capabilities", "--json"], None)
    _require(data, ("supported_relation_types", "relation_support_by_language"), "capabilities")
    return data


def index(repo: PathLike, *, profile: Profile = "full", force: bool = False) -> dict[str, Any]:
    args = ["index", "--profile", profile, "--format", "json"] + (["--force"] if force else [])
    return _json(args, repo)


def stats(repo: PathLike) -> str:
    """Human-facing token-savings report (evidence for the Context focus area)."""
    return _run(["stats"], repo)


# ---- localisation ---------------------------------------------------------------------------


def search(
    repo: PathLike,
    query: str,
    *,
    top_k: int = 10,
    profile: Profile = "full",
    max_context_bytes: int = 24576,
    head: bool = False,
) -> dict[str, Any]:
    args = ["search", "--query", query, "--format", "json", "--top-k", str(top_k),
            "--profile", profile, "--max-context-bytes", str(max_context_bytes)]  # fmt: skip
    return _json(args + (["--head"] if head else []), repo)


def context(
    repo: PathLike,
    query: str,
    *,
    top_k: int = 10,
    max_context_bytes: int = 24576,
    profile: Profile = "full",
) -> str:
    """Budgeted, agent-ready context for a symptom (``search --format agent``; spec §4.4.2)."""
    args = ["search", "--query", query, "--format", "agent", "--top-k", str(top_k),
            "--profile", profile, "--max-context-bytes", str(max_context_bytes)]  # fmt: skip
    return _run(args, repo)


def neighbors(
    repo: PathLike,
    symbol: str,
    *,
    relation: str = "CALLS",
    direction: Literal["in", "out", "both"] = "both",
    depth: int = 1,
    limit: int = 20,
    exclude_tests: bool = False,
    internal_only: bool = False,
) -> dict[str, Any]:
    args = ["neighbors", "--symbol", symbol, "--relation", relation, "--direction", direction,
            "--depth", str(depth), "--limit", str(limit), "--format", "json"]  # fmt: skip
    args += ["--exclude-tests"] if exclude_tests else []
    args += ["--internal-only"] if internal_only else []
    return _json(args, repo)


def callers(repo: PathLike, symbol: str, **kw: Any) -> dict[str, Any]:
    return neighbors(repo, symbol, direction="in", **kw)


def callees(repo: PathLike, symbol: str, **kw: Any) -> dict[str, Any]:
    return neighbors(repo, symbol, direction="out", **kw)


def impact(
    repo: PathLike, symbol: str, *, depth: int = 2, limit: int = 15, exclude_tests: bool = True
) -> dict[str, Any]:
    args = ["impact", "--symbol", symbol, "--depth", str(depth), "--limit", str(limit),
            "--format", "json"] + (["--exclude-tests"] if exclude_tests else [])  # fmt: skip
    return _json(args, repo)


def definition(repo: PathLike, name: str, *, members: int = 15) -> dict[str, Any]:
    """``entire graph def``: what a name is, with its fields and method surface."""
    return _json(["def", name, "--members", str(members), "--format", "json"], repo)


def explain(repo: PathLike, failure_output: str, *, max_symbols: int = 8) -> str:
    """Declarations of the symbols a failing test or build names (stdin → text)."""
    args = ["explain", "--format", "text", "--max-symbols", str(max_symbols), "--no-echo"]
    return _run(args, repo, input_text=failure_output)


# ---- change analysis ------------------------------------------------------------------------


def diff(repo: PathLike, base: str, head: str, *, max_seconds: int = 120) -> dict[str, Any]:
    args = ["diff", "--base", base, "--head", head, "--json", "--max-seconds", str(max_seconds)]
    return _json(args, repo)


def commit(repo: PathLike, rev: str = "HEAD") -> dict[str, Any]:
    return _json(["commit", rev, "--json"], repo)


def checkpoint(repo: PathLike, checkpoint_id: str) -> dict[str, Any]:
    return _json(["checkpoint", checkpoint_id, "--json"], repo)


def verify(
    repo: PathLike,
    test_command: str,
    *,
    record_baseline: PathLike | None = None,
    pre_edit_baseline: PathLike | None = None,
    timeout_s: float = 1800.0,
    extra_env: Mapping[str, str] | None = None,
) -> str:
    """Gate G3: which tests changed state. Record a baseline first, then compare against it.

    The test command must print per-test results (``pytest -rA`` or ``-v``): with ``-q`` the
    plugin cannot parse ids and degrades to an exit-code-only verdict (amendment A-48).
    ``extra_env`` reaches the test run (for example ``PYTHONPATH`` of the code under test)."""
    if (record_baseline is None) == (pre_edit_baseline is None):
        raise ValueError("pass exactly one of record_baseline or pre_edit_baseline")
    args = ["verify", "--test", test_command]
    if record_baseline is not None:
        args += ["--record-baseline", os.fspath(record_baseline)]
    else:
        args += ["--pre-edit-baseline", os.fspath(pre_edit_baseline)]  # type: ignore[arg-type]
    return _run(args, repo, timeout_s=timeout_s, extra_env=extra_env)


# ---- snapshot (spike A9) ----------------------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    header: dict[str, Any]
    files: list[dict[str, Any]] = field(default_factory=list)
    externals: list[dict[str, Any]] = field(default_factory=list)
    symbols: list[dict[str, Any]] = field(default_factory=list)
    relations: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    def symbol(self, qualified_name: str) -> dict[str, Any]:
        matches = [s for s in self.symbols if s["qualified_name"] == qualified_name]
        if len(matches) != 1:
            raise KeyError(f"{qualified_name}: {len(matches)} matches")
        return matches[0]

    def to_code_graph(
        self,
        *,
        attrs: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
        side_effect_tools: Iterable[str] = (),
        source_root: PathLike | None = None,
        transitive_calls: bool = True,
    ) -> nx.DiGraph:
        """The motif engine's graph: symbols as nodes, relation-type sets as edges.

        ``side_effect_tools`` are qualified names from the host's tool registry
        (``side_effect: true``). With ``source_root``, class symbols carry their source so the
        deterministic ``has_mutable_accumulator`` scan can run on real code.
        """
        writers = set(side_effect_tools)
        symbols = []
        for record in self.symbols:
            extra: dict[str, Any] = {"qualified_name": record["qualified_name"]}
            if record["qualified_name"] in writers:
                extra["side_effect"] = True
            if source_root is not None and record["kind"] == "class":
                extra["source"] = _source_of(Path(source_root), record)
            if attrs is not None:
                extra.update(attrs(record))
            symbols.append(
                Symbol(record["id"], record["kind"], record["name"], record["file_path"], extra)
            )
        relations = [Relation(r["from_id"], r["to_id"], r["type"]) for r in self.relations]
        graph = build_graph(symbols, relations)
        if transitive_calls:
            add_transitive_calls(graph)
        return graph


def _source_of(root: Path, record: Mapping[str, Any]) -> str:
    lines = (root / record["file_path"]).read_text(encoding="utf-8").splitlines()
    return textwrap.dedent("\n".join(lines[record["start_line"] - 1 : record["end_line"]]))


def parse_snapshot(lines: Iterable[str]) -> Snapshot:
    """Strict parser for ``entire graph snapshot --format ndjson`` (schema 1.1)."""
    records = [json.loads(line) for line in lines if line.strip()]
    if not records:
        raise GraphSchemaError("empty snapshot")
    header, *body = records
    _require(header, ("schema_version", "provider", "provider_version"), "header")
    if header["schema_version"] != SNAPSHOT_SCHEMA or header["provider"] != "entire-graph":
        raise GraphSchemaError(f"unsupported snapshot header: {json.dumps(header)[:200]}")
    snapshot = Snapshot(header=header)
    for record in body:
        kind = record.get("record_type")
        if kind not in _RECORD_TYPES:
            raise GraphSchemaError(f"unknown record_type {kind!r}")
        if kind == "symbol":
            _require(record, _SYMBOL_FIELDS, "symbol")
            snapshot.symbols.append(record)
        elif kind == "relation":
            _require(record, _RELATION_FIELDS, "relation")
            snapshot.relations.append(record)
        elif kind == "file":
            _require(record, ("id", "path", "language"), "file")
            snapshot.files.append(record)
        elif kind == "external":
            _require(record, ("id", "kind", "value"), "external")
            snapshot.externals.append(record)
        else:
            _require(record, ("stats", "completeness"), "summary")
            snapshot.summary.update(record)
    if not snapshot.summary:
        raise GraphSchemaError("snapshot has no summary record (truncated stream?)")
    return snapshot


def snapshot(repo: PathLike, *, worktree: bool = True, profile: Profile = "full") -> Snapshot:
    args = ["snapshot", "--format", "ndjson", "--profile", profile]
    args += ["--worktree"] if worktree else []
    return parse_snapshot(_run(args, repo).splitlines())


def normalise_snapshot(text: str) -> str:
    """Golden-file form: machine-specific header fields blanked, records in a stable order."""
    records = [json.loads(line) for line in text.splitlines() if line.strip()]
    header, *body = records
    header = {**header, "repo_root": "<repo>", "commit": "<commit>", "tree": "<tree>"}
    order = {kind: i for i, kind in enumerate(_RECORD_TYPES)}

    def key(record: Mapping[str, Any]) -> tuple[int, str]:
        edge = f"{record.get('from_id')}|{record.get('type')}|{record.get('to_id')}"
        return order[record["record_type"]], str(record.get("id") or edge)

    lines = [json.dumps(header, sort_keys=True)]
    lines += [json.dumps(record, sort_keys=True) for record in sorted(body, key=key)]
    return "\n".join(lines) + "\n"
