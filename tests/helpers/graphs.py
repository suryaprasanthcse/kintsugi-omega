"""Source trees behind the golden graph snapshots, and host graphs built from them.

The Stage 2 goldens (``ledger_before``, ``ledger_after``, ``scout``) have their own small source
trees under ``tests/fixtures/graph``. The Stage 7 positives reuse the disclosed practice hosts:
``crm_practice`` is the CRM practice host as committed (its latent defect included), and
``ledger_practice_before`` is the practice ledger with the Stage 6 practice regression applied
(the origin of AB-1 before its fix), assembled from the two fixtures rather than copied.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import networkx as nx

from kintsugi.memory import graph, predicates

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
GRAPH = FIXTURES / "graph"
REHEARSAL = FIXTURES / "rehearsal"
GOLDEN = GRAPH / "snapshots"

# name → (package dir inside the tree, the host registry's side-effecting tools)
HOSTS: dict[str, tuple[str, set[str]]] = {
    "ledger_before": ("ledger", {"post_journal_entry"}),
    "ledger_after": ("ledger", {"post_journal_entry"}),
    "scout": ("scout", set()),
    "crm_practice": ("practice_crm", {"create_contact"}),
    "ledger_practice_before": ("practice_ledger", {"post_line"}),
}


def source_tree(name: str, destination: Path) -> Path:
    """Materialise the source tree of golden ``name`` at ``destination``."""
    ignore = shutil.ignore_patterns("__pycache__", ".pytest_cache")
    if name == "crm_practice":
        shutil.copytree(REHEARSAL / "crm_practice", destination, ignore=ignore)
    elif name == "ledger_practice_before":
        shutil.copytree(REHEARSAL / "ledger_practice", destination, ignore=ignore)
        shutil.copy2(REHEARSAL / "refactor" / "reconcile.py",
                     destination / "practice_ledger" / "reconcile.py")  # fmt: skip
    else:
        shutil.copytree(GRAPH / name, destination, ignore=ignore)
    return destination


def snapshot(name: str) -> graph.Snapshot:
    text = (GOLDEN / f"{name}.ndjson").read_text(encoding="utf-8")
    return graph.parse_snapshot(text.splitlines())


def host_graph(name: str, sources: Path) -> nx.DiGraph:
    """The motif engine's graph of golden ``name`` (``sources``: its materialised tree)."""
    package, tools = HOSTS[name]
    return predicates.host_graph(
        snapshot(name), package_dir=package, side_effect_tools=tools, source_root=sources
    )
