"""Predicates on real source, host graphs from one snapshot, and the Antibody Library (§5.8)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kintsugi.memory import graph, predicates
from kintsugi.memory import motif as m
from kintsugi.memory.antibody import Antibody, AntibodyState, MotifSpec
from kintsugi.memory.library import AntibodyLibrary, LibraryError, similarity

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "graph"
CORE: dict[str, Any] = {
    "nodes": {
        "R": {"kind": "function", "pred": ["is_retry_wrapper"]},
        "Enc": {"kind": "function"},
        "F": {"kind": "function"},
        "S": {"kind": "class", "pred": ["has_mutable_accumulator"]},
    },
    "edges": [["Enc", "R", "CALLS"], ["Enc", "F", "CONTAINS"], ["F", "S", "CONSTRUCTS"]],
}

RETRY = """
def retry_with_backoff(fn, attempts=5):
    for attempt in range(attempts):
        try:
            return fn()
        except RateLimited:
            if attempt == attempts - 1:
                raise
"""
NOT_RETRY = """
def retry_label(issue):
    return "retry: " + issue
"""


def _snapshot(name: str) -> graph.Snapshot:
    text = (FIXTURES / "snapshots" / f"{name}.ndjson").read_text(encoding="utf-8")
    return graph.parse_snapshot(text.splitlines())


def _host(name: str, package: str, tools: set[str]) -> Any:
    return predicates.host_graph(
        _snapshot(name), package_dir=package, side_effect_tools=tools,
        source_root=FIXTURES / name,
    )  # fmt: skip


def test_retry_wrappers_are_confirmed_on_their_source() -> None:
    assert predicates.scan_retry_wrapper(RETRY, "retry_with_backoff") is m.Verdict.TRUE
    assert predicates.scan_retry_wrapper(NOT_RETRY, "retry_label") is m.Verdict.FALSE
    assert predicates.scan_retry_wrapper("def (:", "x") is m.Verdict.INCONCLUSIVE


def test_host_graphs_carry_sources_tools_and_only_the_package() -> None:
    before = _host("ledger_before", "ledger", {"post_journal_entry"})
    by_name = {d["qualified_name"]: n for n, d in before.nodes(data=True)}
    assert before.nodes[by_name["post_journal_entry"]].get("side_effect") is True
    assert "class ReconcileSession" in before.nodes[by_name["ReconcileSession"]]["source"]
    assert "def retry_with_backoff" in before.nodes[by_name["retry_with_backoff"]]["source"]
    assert not predicates.host_graph(
        _snapshot("ledger_before"), package_dir="elsewhere", side_effect_tools=(),
        source_root=FIXTURES / "ledger_before",
    ).nodes  # fmt: skip


def test_the_registry_reproduces_the_reconciled_motif_on_real_code() -> None:
    matcher = m.MotifMatcher(MotifSpec.model_validate(CORE), predicates=predicates.registry())
    before = _host("ledger_before", "ledger", {"post_journal_entry"})
    [embedding] = matcher.matches(before)
    assert predicates.names(before, embedding) == {
        "R": "retry_with_backoff", "Enc": "run_reconcile", "F": "attempt",
        "S": "ReconcileSession",
    }  # fmt: skip
    assert not matcher.matches_any(_host("ledger_after", "ledger", {"post_journal_entry"}))
    assert matcher.matches_any(_host("scout", "scout", set()))  # the disclosed look-alike


def test_inconclusive_accumulators_go_to_the_classifier_once() -> None:
    asked: list[tuple[str, str]] = []

    def classify(predicate: str, name: str, source: str) -> bool | None:
        asked.append((predicate, name))
        return True

    cache = predicates.VerdictCache()
    check = predicates.registry(classify=classify, cache=cache)["has_mutable_accumulator"]
    g = m.build_graph(
        [m.Symbol("s", "class", "Opaque", attrs={"qualified_name": "Opaque",
                  "source": "class Opaque:\n    def __init__(self):\n        self.x = make()\n"
                            "    def add(self, v):\n        self.x.append(v)\n"})], [])  # fmt: skip
    assert m.scan_mutable_accumulator(g.nodes["s"]["source"], "Opaque") is m.Verdict.FALSE
    # FALSE is conclusive: no classifier call. An inconclusive class (fields, no mutation seen)
    # is asked exactly once, then served from the cache.
    assert check(g, "s") is False
    g.nodes["s"]["source"] = "class Opaque:\n    items: list = []\n"
    assert m.scan_mutable_accumulator(g.nodes["s"]["source"], "Opaque") is m.Verdict.INCONCLUSIVE
    assert check(g, "s") is True
    assert check(g, "s") is True
    assert asked == [("has_mutable_accumulator", "Opaque")]


# ---- the library -------------------------------------------------------------------------------


def _antibody(state: AntibodyState = AntibodyState.CANDIDATE) -> Antibody:
    return Antibody.model_validate({
        "id": "AB-1", "version": "1.0", "state": state,
        "signature": {"hash": "sha256:abc", "fields": {
            "error_class": "DuplicateEntry", "failing_span": ["CHAIN", "grader.verdict"],
            "tool": "books:/lines", "preceding": ["fx_rate", "books:/lines", "books:/lines"]}},
        "receptor": {"type": "regex", "rule": '^"?FAIL:DUPLICATE_'},
        "motif": CORE,
        "recipe": "Move the construction of {S} out of the retried scope.",
    })  # fmt: skip


def test_save_load_index_and_pinned_versions(tmp_path: Path) -> None:
    lib = AntibodyLibrary(tmp_path / "antibodies")
    assert lib.next_id() == "AB-1"
    lib.save(_antibody(), receptor_body={"name": "r"}, evidence={"origin_incident": "INC-1"},
             description="state reset on retry", event={"event": "synthesized"})  # fmt: skip
    folder = tmp_path / "antibodies" / "AB-1"
    for name in ("antibody.yaml", "receptor.json", "motif.yaml", "recipe.md", "contract.yaml",
                 "evidence.json", "lineage.json", "versions/1.0.yaml"):  # fmt: skip
        assert (folder / name).is_file(), name
    assert lib.next_id() == "AB-2"
    assert lib.index()["signatures"] == {"sha256:abc": "AB-1@1.0"}
    revised = lib.load("AB-1").revise("motif", preconditions=["side effect in scope"])
    lib.save(revised, event={"event": "refined"})
    assert lib.load("AB-1").version == "1.1"
    assert lib.ref("AB-1@1.0").preconditions == []
    assert lib.ref("AB-1@1.1").preconditions == ["side effect in scope"]
    assert lib.index()["signatures"]["sha256:abc"] == "AB-1@1.1"
    with pytest.raises(LibraryError):
        lib.ref("AB-1@2.0")
    events = [e["event"] for e in lib.lineage("AB-1")["events"]]
    assert events == ["synthesized", "refined"]


def test_lifecycle_moves_are_logged_and_quarantines_counted(tmp_path: Path) -> None:
    lib = AntibodyLibrary(tmp_path)
    lib.save(_antibody())
    for state in (AntibodyState.MATURED, AntibodyState.ACTIVE, AntibodyState.QUARANTINED):
        lib.transition("AB-1", state, why=f"to {state}")
    with pytest.raises(ValueError, match="not a lifecycle transition"):
        lib.transition("AB-1", AntibodyState.MATURED, why="no")
    ab = lib.load("AB-1")
    assert ab.state is AntibodyState.QUARANTINED
    assert ab.lineage.quarantines == 1
    moves = [(e["from"], e["to"]) for e in lib.lineage("AB-1")["events"]]
    assert moves[-1] == ("ACTIVE", "QUARANTINED")
    lib.protect("AB-1", "crm-practice")
    lib.unprotect("AB-1", "crm-practice", why="reverted")
    assert lib.load("AB-1").lineage.hosts_protected == []


def test_retrieval_needs_an_active_antibody(tmp_path: Path) -> None:
    lib = AntibodyLibrary(tmp_path)
    lib.save(_antibody())
    fields = _antibody().signature.fields
    assert lib.retrieve("sha256:abc", fields) is None  # a CANDIDATE is never applied
    lib.transition("AB-1", AntibodyState.MATURED, why="matured")
    lib.transition("AB-1", AntibodyState.ACTIVE, why="receptor precise")
    exact = lib.retrieve("sha256:abc", fields)
    assert exact is not None
    assert (exact.how, exact.similarity) == ("exact", 1.0)
    near = {**fields, "preceding": ["fx_rate", "fx_rate#retry", "books:/lines"]}
    semantic = lib.retrieve("sha256:other", near)
    assert semantic is not None
    assert semantic.how == "semantic"
    assert semantic.similarity >= 0.85
    other_host = {**fields, "tool": "crm:/contacts", "preceding": ["geo"]}
    assert similarity(fields, other_host) < 0.85
    assert lib.retrieve("sha256:other", other_host) is None
    lib.transition("AB-1", AntibodyState.QUARANTINED, why="canary rejected")
    assert lib.retrieve("sha256:abc", fields) is None
