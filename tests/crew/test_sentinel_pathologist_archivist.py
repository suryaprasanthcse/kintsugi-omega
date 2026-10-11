"""The Sentinel (paging rule, I7 dispositions), the Pathologist (φ clusters, symptom, G0) and the
Archivist (blame, checkpoint mapping, editing turns, verified intent records)."""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kintsugi.control import contracts as c
from kintsugi.control.activities import InvalidAlert
from kintsugi.crew import archivist as ar
from kintsugi.crew.context import CrewContext, LocalTraces
from kintsugi.crew.llm import ScriptedModel
from kintsugi.crew.pathologist import Pathologist
from kintsugi.crew.sentinel import Sentinel, watch_record
from kintsugi.gates.checkout import clean_checkout
from kintsugi.gates.evals import run_tasks
from kintsugi.gates.target import load_target
from kintsugi.senses.detections import Binding
from kintsugi.senses.signature import from_chaos_log, tool_names
from kintsugi.senses.store import HitEvent, SenseStore
from tests.helpers import checkpoints
from tests.helpers import practice as pr

T0 = datetime(2026, 10, 12, 3, 0, tzinfo=UTC)
BINDING = Binding("det-r7", "R-007", "ledger-practice", 1, threshold_count=3, window_minutes=10)


def _hit(store: SenseStore, n: int, minutes: float) -> c.Hit:
    at = (T0 + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
    event = HitEvent(
        f"k{n}",
        "det-r7",
        f"t{n}",
        f"s{n}",
        "poller",
        detected_at=at,
        receptor="R-007",
        host="ledger-practice",
    )
    assert store.claim_hit(event)
    return c.Hit(f"k{n}", "det-r7", f"t{n}", f"s{n}", "poller", "R-007", "ledger-practice", at)


@pytest.fixture(scope="module")
def practice(tmp_path_factory: pytest.TempPathFactory) -> dict:
    p = pr.make_repo(tmp_path_factory.mktemp("practice") / "repo")
    buggy = pr.plant_refactor(p.repo)
    return {"repo": p.repo, "healthy": p.healthy, "buggy": buggy}


def _ctx(tmp_path: Path, repo: Path, **kw) -> CrewContext:
    target = load_target(repo, pr.HOST)
    return CrewContext(state=tmp_path / "crew", targets={pr.HOST: target}, **kw)


def test_the_sentinel_holds_below_threshold_then_pages_and_attaches(
    tmp_path: Path, practice: dict
) -> None:
    store = SenseStore(":memory:")
    ctx = _ctx(
        tmp_path,
        practice["repo"],
        store=store,
        bindings={"det-r7": BINDING},
        clock=lambda: (T0 + timedelta(minutes=30)).timestamp(),
    )
    sentinel = Sentinel(ctx)
    first = sentinel.route(_hit(store, 1, 0), [])
    second = sentinel.route(_hit(store, 2, 2), [])
    assert (first.action, second.action) == ("drop", "drop")
    assert store.seen("k1").disposition == "held"  # type: ignore[union-attr]
    assert store.seen("k1").incident == watch_record("R-007", "ledger-practice")  # type: ignore[union-attr]
    third = sentinel.route(_hit(store, 3, 4), [])
    assert third.action == "open"
    assert third.incident_id == "INC-0001"
    assert {h.hit_key: h.disposition for h in store.seen_hits().values()} == {
        "k1": "attached",
        "k2": "attached",
        "k3": "opened",
    }
    attached = sentinel.route(_hit(store, 4, 5), ["INC-0001"])
    assert (attached.action, attached.incident_id) == ("attach", "INC-0001")
    opened = ctx.case("INC-0001").reader("sentinel").get("incident_open", "open")
    assert opened["hits"] == ["k1", "k2", "k3"]
    assert opened["severity"] in {"low", "warning", "high"}
    assert opened["var_estimate"] > 0
    late = sentinel.route(_hit(store, 5, 40), [])  # outside every window, no open incident
    assert late.action == "drop"


def test_the_sentinel_asks_haiku_for_severity(tmp_path: Path, practice: dict) -> None:
    store = SenseStore(":memory:")
    model = ScriptedModel({"sentinel": {"severity": "high", "summary": "duplicates"}})
    ctx = _ctx(
        tmp_path,
        practice["repo"],
        store=store,
        model=model,
        bindings={"det-r7": BINDING.__class__(**{**vars(BINDING), "threshold_count": 1})},
    )
    decision = Sentinel(ctx).route(_hit(store, 1, 0), [])
    assert decision.severity == "high"
    assert model.calls[0].model_id == "claude-haiku-5-5"
    assert "trace t1" in model.calls[0].prompt
    assert "def " not in model.calls[0].prompt  # hits only, never code


def _observe(practice: dict, tmp_path: Path) -> LocalTraces:
    target = load_target(practice["repo"], pr.HOST)
    train = importlib.import_module("practice_ledger.evals.generate").tasks("train")
    with clean_checkout(practice["repo"], practice["buggy"]) as checkout:
        live = run_tasks(target, checkout, train, seed=1, phase="live")
    observations = from_chaos_log(
        live.chaos_log,
        live.verdicts(),
        host=pr.HOST,
        split="train",
        seed=1,
        run="live1",
        phase="live",
        services=tool_names(target.host.tools),
        detection_ids=["det-r7"],
    )
    LocalTraces.write(tmp_path / "obs", "live1", observations)
    return LocalTraces(tmp_path / "obs")


def test_the_pathologist_clusters_describes_and_reproduces(tmp_path: Path, practice: dict) -> None:
    traces = _observe(practice, tmp_path)
    ctx = _ctx(tmp_path, practice["repo"], traces=traces)
    hits = tuple(
        c.Hit(f"k-{o.trace_id}", "det-r7", o.trace_id, "s", "poller", "R-007", pr.HOST)
        for o in traces._index().values()
    )
    alert = c.Alert("INC-0007", pr.HOST, hits)
    sig = Pathologist(ctx).triage(alert)
    assert sig.phi.startswith("sha256:")
    assert 0 < len(sig.summary) <= 800
    assert "DuplicateEntry" in sig.summary
    symptom = ctx.case("INC-0007").reader("archivist").get("symptom", "summary")
    assert symptom["summary"] == sig.summary
    repro = Pathologist(ctx).reproduce(sig)
    assert repro.ok
    assert (repro.failures, repro.runs) == (3, 3)
    record = ctx.case("INC-0007").tool_read("repro", "g0", tool="test")
    assert record["cassette_sha"] == repro.cassette_sha
    assert record["seed"] == 1
    with pytest.raises(InvalidAlert):
        Pathologist(ctx).triage(c.Alert("INC-0008", pr.HOST, ()))


# ---- Archivist ------------------------------------------------------------------------------

PROMPT = (
    "Refactor reconcile's retry logic for readability. Keep behavior identical: this is "
    "purely a readability refactor. Apply the change and run the tests."
)


def _transcript(path: str) -> bytes:
    entries = [
        {"type": "user", "message": {"role": "user", "content": PROMPT}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "I'll name the retried pass."},
                    {"type": "tool_use", "name": "Edit", "input": {"file_path": f"C:/wt/{path}"}},
                ]
            },
        },
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
        {"type": "user", "message": {"role": "user", "content": "Now update the README."}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Write", "input": {"file_path": "README.md"}}
                ]
            },
        },
    ]
    return b"".join(json.dumps(e).encode() + b"\n" for e in entries)


def test_editing_turns_quotes_and_heuristics() -> None:
    full = _transcript(pr.RECONCILE)
    turns = ar.editing_turns(full, pr.RECONCILE)
    assert [t["prompt"] for t in turns] == [PROMPT]  # the README turn is not selected
    intent = ar.heuristic_intent(turns)
    assert intent["stated_goal"].startswith("Refactor reconcile's retry logic")
    assert [c_["quote"] for c_ in intent["behavioral_contract"]] == [
        "Keep behavior identical: this is purely a readability refactor."
    ]
    text = ar.decoded_text(full)
    kept, dropped = ar.verify_quotes(
        [
            {"clause": "same", "quote": "Keep behavior identical"},
            {"clause": "invented", "quote": "never touch the cursor"},
            {"clause": "long", "quote": "x" * 301},
        ],
        text,
    )
    assert [k["clause"] for k in kept] == ["same"]
    assert [d["clause"] for d in dropped] == ["invented", "long"]


def test_blame_names_the_refactor_as_the_originating_change(practice: dict) -> None:
    repo = Path(practice["repo"])
    with clean_checkout(repo, practice["buggy"]) as checkout:
        lines = ar.blame(checkout, pr.RECONCILE, 70, 80)
        root = pr.git(checkout, "rev-list", "--max-parents=0", "HEAD")
    assert ar.originating_commit(lines, root) == practice["buggy"]


@pytest.mark.contract
def test_the_archivist_writes_a_verified_intent_record(tmp_path: Path) -> None:
    p = pr.make_repo(tmp_path / "repo")
    pr.write(p.repo, pr.RECONCILE, pr.REFACTOR.read_text(encoding="utf-8"))
    checkpoint = "01M4ARCH1V1STTESTCHECKP01T"
    checkpoints.write_checkpoint(
        p.repo, checkpoint, _transcript(pr.RECONCILE), files_touched=[pr.RECONCILE]
    )
    pr.git(p.repo, "add", "-A")
    pr.git(
        p.repo,
        *pr.IDENTITY,
        "commit",
        "-q",
        "--no-verify",
        "-m",
        f"refactor(reconcile): name the retried pass\n\nEntire-Checkpoint: {checkpoint}",
    )
    traces = _observe({"repo": p.repo, "buggy": pr.git(p.repo, "rev-parse", "HEAD")}, tmp_path)
    ctx = _ctx(tmp_path, p.repo, traces=traces)
    hits = tuple(
        c.Hit(f"k-{o.trace_id}", "det-r7", o.trace_id, "s", "poller", "R-007", pr.HOST)
        for o in traces._index().values()
    )
    path = Pathologist(ctx)
    sig = path.triage(c.Alert("INC-0009", pr.HOST, hits))
    repro = path.reproduce(sig)
    cause = ar.Archivist(ctx).attribute(sig, repro)
    record = ctx.case("INC-0009").reader("coordinator").get("intent_record", "record")
    report = ctx.case("INC-0009").reader("coordinator").get("cause", "report")
    assert record["checkpoint"] == checkpoint
    assert record["behavioral_contract"]
    assert all(c_["quote"] in PROMPT for c_ in record["behavioral_contract"])
    assert report["regression"] is True
    assert report["first_bad_checkpoint"] == checkpoint
    assert cause.suspects
