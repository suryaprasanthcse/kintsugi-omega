"""Spike A9 lock: the live entire-graph output equals the committed golden snapshots.

Regenerate the goldens with ``KINTSUGI_CONTRACT=1 KINTSUGI_REGEN_GOLDENS=1 pytest tests/contract``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kintsugi.memory import graph, motif
from tests.helpers.graphs import source_tree

from .scratch import make_repo

pytestmark = pytest.mark.contract

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "graph"
GOLDEN = FIXTURES / "snapshots"


def test_plugin_is_pinned_and_local_only() -> None:
    graph.require_pinned()
    assert graph.doctor_ok(graph.doctor())


def test_python_relations_match_the_motif_engine() -> None:
    allowed = motif.allowed_relations(graph.capabilities())
    assert allowed >= motif.PYTHON_RELATIONS
    assert {"READS_FIELD", "WRITES_FIELD", "ACCESSES", "IMPLEMENTS"}.isdisjoint(allowed)


@pytest.mark.parametrize(
    "name", ["ledger_before", "ledger_after", "scout", "crm_practice", "ledger_practice_before"]
)
def test_snapshot_matches_golden(name: str, tmp_path: Path) -> None:
    # The directory name fixes the repo_key.
    repo = make_repo(tmp_path / name, source_tree(name, tmp_path / "src" / name))
    live = graph.normalise_snapshot(
        graph._run(["snapshot", "--format", "ndjson", "--profile", "full", "--worktree"], repo)
    )
    golden = GOLDEN / f"{name}.ndjson"
    if os.environ.get("KINTSUGI_REGEN_GOLDENS") == "1":
        GOLDEN.mkdir(parents=True, exist_ok=True)
        golden.write_text(live, encoding="utf-8", newline="\n")
    assert live == golden.read_text(encoding="utf-8")
