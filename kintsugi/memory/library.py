"""The Antibody Library (spec §5.8): memory B cells, in the monorepo's ``antibodies/`` (D3).

Layout (§5.8.1)::

    antibodies/
      index.json             signature φ → AB-id@version; per antibody its state and description
      AB-1/
        antibody.yaml        the spec (Appendix E.1), current version
        receptor.json        the neatlogs Detection body as drafted (and created, when it is)
        motif.yaml           the motif (§4.4.3)
        recipe.md            the Claude Code instruction template ({intent} and {scope} slots)
        contract.yaml        gate thresholds
        evidence.json        incident, traces, checkpoints, statistics, CI and PR links
        lineage.json         versions, state changes, quarantines, refinements, hosts protected
        versions/1.0.yaml    every version's antibody.yaml, so ``AB-1@1.0`` stays resolvable

Retrieval (§5.8.3): an exact φ match first; then a deterministic semantic fallback over the
signature's fields (error class, failing span, tool, preceding 3-gram) accepted at θ = 0.85;
then the caller confirms structurally (motif on the host's graph, preconditions) and requires
``state = ACTIVE``. Anything else is a primary response.

The library is written by the Immunologist's activities (tool-written files, A-1); every state
change is a lineage event, so the proof bundle can replay how an antibody got where it is.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from kintsugi.memory.antibody import Antibody, AntibodyState, AntibodyVersion

THETA = 0.85
_FIELD_WEIGHTS = {"error_class": 0.4, "failing_span": 0.2, "tool": 0.2, "preceding": 0.2}
_lock = threading.Lock()


class LibraryError(RuntimeError):
    pass


def _dump_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


def _dump_yaml(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def similarity(a: Mapping[str, Any], b: Mapping[str, Any]) -> float:
    """Deterministic semantic similarity of two signatures' fields, in [0, 1]."""
    score = 0.0
    for name, weight in _FIELD_WEIGHTS.items():
        left, right = a.get(name), b.get(name)
        if name == "preceding":
            x, y = set(left or []), set(right or [])
            score += weight * (len(x & y) / len(x | y) if x | y else 1.0)
        elif left is not None and left == right:
            score += weight
    return round(score, 4)


@dataclass(frozen=True)
class Retrieval:
    antibody: Antibody
    how: str  # "exact" | "semantic"
    similarity: float


class AntibodyLibrary:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # ---- reading ------------------------------------------------------------------------------

    def ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        found = [p.name for p in self.root.iterdir() if (p / "antibody.yaml").is_file()]
        return sorted(found, key=lambda i: int(i.split("-")[1]))

    def next_id(self) -> str:
        numbers = [int(i.split("-")[1]) for i in self.ids()]
        return f"AB-{max(numbers, default=0) + 1}"

    def load(self, ab_id: str, version: str | None = None) -> Antibody:
        folder = self.root / ab_id
        path = folder / "antibody.yaml"
        if version is not None:
            path = folder / "versions" / f"{AntibodyVersion.parse(version)}.yaml"
        if not path.is_file():
            raise LibraryError(f"no antibody {ab_id}" + (f"@{version}" if version else ""))
        return Antibody.from_yaml(path.read_text(encoding="utf-8"))

    def ref(self, ref: str) -> Antibody:
        """``AB-1@1.0`` (pinned) or ``AB-1`` (current)."""
        ab_id, _, version = ref.partition("@")
        return self.load(ab_id, version or None)

    def index(self) -> dict[str, Any]:
        path = self.root / "index.json"
        if not path.is_file():
            return {"signatures": {}, "antibodies": {}}
        return json.loads(path.read_text(encoding="utf-8"))

    def lineage(self, ab_id: str) -> dict[str, Any]:
        path = self.root / ab_id / "lineage.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def evidence(self, ab_id: str) -> dict[str, Any]:
        path = self.root / ab_id / "evidence.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def receptor_body(self, ab_id: str) -> dict[str, Any]:
        path = self.root / ab_id / "receptor.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def recipe(self, ab_id: str) -> str:
        return (self.root / ab_id / "recipe.md").read_text(encoding="utf-8")

    def active(self) -> list[Antibody]:
        return [ab for i in self.ids() if (ab := self.load(i)).state is AntibodyState.ACTIVE]

    # ---- retrieval (§5.8.3 steps 1–2) -----------------------------------------------------------

    def retrieve(
        self, phi: str, fields: Mapping[str, Any], *, theta: float = THETA
    ) -> Retrieval | None:
        """The antibody whose signature matches φ exactly, or semantically at ≥ θ.

        Only ACTIVE antibodies are returned: a CANDIDATE is unproven and a QUARANTINED one must
        not be applied (its receptor stays on, its recipe does not)."""
        exact = self.index()["signatures"].get(phi)
        if exact is not None:
            ab = self.ref(str(exact).split("@")[0])
            if ab.state is AntibodyState.ACTIVE:
                return Retrieval(ab, "exact", 1.0)
        best: Retrieval | None = None
        for ab in self.active():
            score = similarity(fields, ab.signature.fields)
            if score >= theta and (best is None or score > best.similarity):
                best = Retrieval(ab, "semantic", score)
        return best

    # ---- writing -------------------------------------------------------------------------------

    def save(
        self,
        ab: Antibody,
        *,
        receptor_body: Mapping[str, Any] | None = None,
        evidence: Mapping[str, Any] | None = None,
        description: str | None = None,
        event: Mapping[str, Any] | None = None,
    ) -> Path:
        """Write ``ab`` (all seven files, the version snapshot, the index) and log ``event``."""
        with _lock:
            folder = self.root / ab.id
            data = ab.model_dump(mode="json")
            _dump_yaml(folder / "antibody.yaml", data)
            _dump_yaml(folder / "versions" / f"{ab.version}.yaml", data)
            _dump_yaml(folder / "motif.yaml", {"motif": ab.motif.model_dump(mode="json")})
            _dump_yaml(folder / "contract.yaml", ab.contract.model_dump(mode="json"))
            (folder / "recipe.md").write_text(ab.recipe.rstrip() + "\n", encoding="utf-8",
                                              newline="\n")  # fmt: skip
            if receptor_body is not None:
                _dump_json(folder / "receptor.json", dict(receptor_body))
            if evidence is not None:
                _dump_json(folder / "evidence.json", {**self.evidence(ab.id), **dict(evidence)})
            lineage = self.lineage(ab.id) or {"id": ab.id, "events": []}
            lineage.update(ab.lineage.model_dump(mode="json"))
            if event is not None:
                lineage["events"].append({"at": _now(), "version": ab.version, **dict(event)})
            _dump_json(folder / "lineage.json", lineage)
            index = self.index()
            index["signatures"][ab.signature.hash] = ab.ref
            entry = index["antibodies"].get(ab.id, {})
            entry.update({"version": ab.version, "state": str(ab.state)})
            if description is not None:
                entry["description"] = description
            index["antibodies"][ab.id] = entry
            _dump_json(self.root / "index.json", index)
            return folder

    def transition(self, ab_id: str, to: AntibodyState, *, why: str, **detail: Any) -> Antibody:
        """A lifecycle move (§5.8.2), logged; QUARANTINED counts a quarantine."""
        current = self.load(ab_id)
        if current.state is to:
            return current
        moved = current.transition(to)
        self.save(moved, event={"event": "state", "from": str(current.state), "to": str(to),
                                "why": why, **detail})  # fmt: skip
        return moved

    def record(self, ab_id: str, event: Mapping[str, Any]) -> None:
        """Append a lineage event without changing the antibody (backfill, PR comments, …)."""
        with _lock:
            path = self.root / ab_id / "lineage.json"
            lineage = self.lineage(ab_id) or {"id": ab_id, "events": []}
            lineage["events"].append({"at": _now(), "version": self.load(ab_id).version,
                                      **dict(event)})  # fmt: skip
            _dump_json(path, lineage)

    def protect(self, ab_id: str, host: str) -> Antibody:
        """``host`` now carries this antibody (maturation or vaccination merged or PR-opened)."""
        ab = self.load(ab_id)
        if host in ab.lineage.hosts_protected:
            return ab
        hosts = sorted({*ab.lineage.hosts_protected, host})
        updated = ab.model_copy(update={"lineage": ab.lineage.model_copy(
            update={"hosts_protected": hosts})})  # fmt: skip
        self.save(updated, event={"event": "protected", "host": host})
        return updated

    def unprotect(self, ab_id: str, host: str, *, why: str) -> Antibody:
        ab = self.load(ab_id)
        hosts = [h for h in ab.lineage.hosts_protected if h != host]
        updated = ab.model_copy(update={"lineage": ab.lineage.model_copy(
            update={"hosts_protected": hosts})})  # fmt: skip
        self.save(updated, event={"event": "unprotected", "host": host, "why": why})
        return updated


_SLUG = re.compile(r"[^a-z0-9]+")


def cassette_slug(text: str) -> str:
    """A file-name-safe slug, e.g. for a trace-to-test cassette."""
    return _SLUG.sub("_", text.lower()).strip("_")[:60] or "case"
