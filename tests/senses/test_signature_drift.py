"""The failure signature φ (§3.4), drift watching and the L4 re-verification policy (§6.4)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kintsugi.sandbox import proc
from kintsugi.senses import signature as sg
from kintsugi.senses.completeness import ReportedHit, completeness
from kintsugi.senses.drift import Drift, MainWatcher, Reverify, reverify_plan
from kintsugi.senses.records import SpanRecord, TraceRecord
from kintsugi.senses.store import HitEvent, SenseStore
from kintsugi.senses.tags import TagSet

T0 = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


def test_errors_map_to_a_canonical_taxonomy() -> None:
    assert sg.canonical_error("HTTP 429 Too Many Requests") == "RateLimited"
    assert sg.canonical_error("RateLimitError: slow down") == "RateLimited"
    assert sg.canonical_error("FAIL:DUPLICATE_ENTRY") == "DuplicateEntry"
    assert sg.canonical_error("FAIL:FX_UNAVAILABLE") == "UpstreamUnavailable"
    assert sg.canonical_error("FAIL:WRONG_AMOUNT") == "WrongAmount"
    assert sg.canonical_error(None) == "Unknown"


def test_normalisation_removes_ids_numbers_times_and_paths() -> None:
    text = ("trace 9f2c4e11-aaaa-bbbb-cccc-0123456789ab at 2026-10-11T12:00:00Z posted 190.40 "
            "to C:\\work\\ledger\\db.sqlite and /tmp/x/y.json sha 3d1f00aa77")  # fmt: skip
    assert sg.normalize(text) == "trace <id> at <ts> posted <n> to <path> and <path> sha <id>"


def _obs(trace: str, task: str, error: str, gram: tuple[str, ...]) -> sg.FailureObservation:
    return sg.FailureObservation(trace, "ledger", "CHAIN", "grader.verdict", "post_journal_entry",
                                 error, gram, ("R-007:ledger:v1",), task, error)  # fmt: skip


def test_phi_ignores_instance_details_and_separates_mechanisms() -> None:
    gram = ("fx_rate", "fx_rate#retry", "post_journal_entry")
    a = _obs("t-1", "inv-001", "FAIL:DUPLICATE_ENTRY", gram)
    b = _obs("t-2", "inv-077", "FAIL:DUPLICATE_ENTRY", gram)
    c = _obs("t-3", "inv-002", "FAIL:DUPLICATE_ENTRY", ("fx_rate", "post_journal_entry", "x"))
    d = _obs("t-4", "inv-003", "FAIL:WRONG_AMOUNT", gram)
    assert a.phi == b.phi
    assert len({a.phi, c.phi, d.phi}) == 3
    assert a.phi.startswith("sha256:")
    v2 = sg.FailureObservation(**{**vars(a), "detection_ids": ("R-007:ledger:v2",)})
    assert v2.phi == a.phi  # a receptor's new version does not split the family
    clusters = sg.cluster([c, a, d, b])
    assert [cl.size for cl in clusters] == [2, 1, 1]
    assert clusters[0].phi == a.phi
    assert sg.representatives(clusters[0], 1)[0].trace_id == "t-1"


def _span(
    name: str, kind: str, at: int, *, error: str | None = None, out: Any = None
) -> SpanRecord:
    return SpanRecord("tr", f"s{at}", None, kind, name, "ERROR" if error else "OK",
                      T0 + timedelta(seconds=at), None, 1.0, 0.0, None, 0, 0, None, out, error,
                      False)  # fmt: skip


def test_phi_from_a_neatlogs_trace() -> None:
    spans = (
        _span("fx_rate", "TOOL", 1),
        _span("fx_rate", "TOOL", 2, error="RateLimited: HTTP 429"),
        _span("fx_rate", "TOOL", 3),
        _span("post_journal_entry", "TOOL", 4),
        _span("grader.verdict", "CHAIN", 5, out="FAIL:DUPLICATE_ENTRY"),
    )
    record = TraceRecord("tr", "task:inv-9", "host:ledger", "sess", "OK", T0, T0, 5.0, 0.0, 0, 4,
                         0, 5, TagSet.parse(["host:ledger", "split:train", "seed:2"]), 1,
                         spans)  # fmt: skip
    obs = sg.from_trace(record, ["R-001:ledger:v1"])
    assert obs is not None
    assert (obs.failing_kind, obs.failing_name) == ("TOOL", "fx_rate")  # the first errored tool
    assert sg.canonical_error(obs.error) == "RateLimited"
    assert obs.preceding == ("fx_rate",)
    assert obs.repro is not None
    assert obs.repro["seed"] == 2


def test_phi_from_a_chaos_log() -> None:
    rows = [
        {"service": "fx", "path": "/rate", "task": "inv-1", "status": 200},
        {"service": "books", "path": "/lines", "task": "inv-1", "status": 201},
        {"service": "fx", "path": "/rate", "task": "inv-1", "status": 429},
        {"service": "fx", "path": "/rate", "task": "inv-1", "status": 200},
        {"service": "books", "path": "/lines", "task": "inv-1", "status": 201},
        {"service": "fx", "path": "/rate", "task": "inv-2", "status": 200},
    ]
    found = sg.from_chaos_log(rows, {"inv-1": "FAIL:DUPLICATE_ENTRY", "inv-2": "PASS"},
                              host="ledger-practice", split="train", seed=3, run="r1",
                              phase="rehearsal", services={"fx": "fx_rate", "books": "post_line"},
                              detection_ids=["omega:R-007:ledger-practice:v1"])  # fmt: skip
    assert len(found) == 1
    obs = found[0]
    assert obs.preceding == ("fx_rate", "fx_rate#retry", "post_line")
    assert obs.tool_name == "post_line"
    assert obs.repro == {"host": "ledger-practice", "task_id": "inv-1", "split": "train",
                         "seed": 3, "run": "r1", "phase": "rehearsal"}  # fmt: skip
    assert sg.tool_names({"fx_rate": {"external": "fx"}, "lookup": {}}) == {"fx": "fx_rate"}


# ---- drift ----------------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return proc.run(["git", "-C", str(repo), *args], timeout_s=60, check=True).stdout.strip()


def test_the_main_watcher_reports_each_move_once(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for args in (("user.name", "t"), ("user.email", "t@example.invalid")):
        _git(repo, "config", *args)
    (repo / "a.txt").write_text("1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "one")
    seen: list[Drift] = []
    state = tmp_path / "heads.json"
    watcher = MainWatcher({"ledger": repo}, seen.append, state_path=state)
    assert watcher.poll_once() == []
    old = _git(repo, "rev-parse", "HEAD")
    (repo / "a.txt").write_text("2\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "two")
    new = _git(repo, "rev-parse", "HEAD")
    assert watcher.poll_once() == [Drift("ledger", old, new)]
    assert watcher.poll_once() == []
    again = MainWatcher({"ledger": repo}, seen.append, state_path=state)  # restart: no replay
    assert again.poll_once() == []
    assert seen == [Drift("ledger", old, new)]


class FakeGraph:
    def __init__(self, diffs: dict[tuple[str, str], list[str]], impacts: dict[str, list[str]]):
        self.diffs, self.impacts = diffs, impacts

    def diff(self, repo: Path, base: str, head: str) -> dict[str, Any]:
        names = self.diffs[(base, head)]
        return {"files": [{"path": "x.py", "changes": [{"name": n} for n in names]}]}

    def impact(self, repo: Path, symbol: str) -> dict[str, Any]:
        def where(name: str) -> str:
            return "tests/test_x.py" if name.startswith("test_") else "ledger/x.py"

        entries = [{"endpoint": {"qualified_name": n, "file_path": where(n)}}
                   for n in self.impacts.get(symbol, [])]  # fmt: skip
        return {"focus": {"qualified_name": symbol, "file_path": "ledger/x.py"},
                "callers": {"entries": entries}}  # fmt: skip


def test_reverify_reruns_g4_only_when_the_radii_intersect(tmp_path: Path) -> None:
    graph = FakeGraph(
        {("old", "new"): ["fmt_amount", "format_amount", "lookup_vendor"],
         ("old", "fix"): ["run_reconcile", "ReconcileSession"],
         ("old", "fix2"): ["parse_invoice"]},
        {"lookup_vendor": ["run_reconcile", "test_lookup", "test_resume"]},
    )  # fmt: skip
    plan = reverify_plan(tmp_path, "old", "new", "fix", graph=graph)
    assert plan.intersection == ("run_reconcile",)
    assert plan.tests == ("test_lookup", "test_resume")
    assert plan.record() == {"G1": "rerun", "G2": "rerun", "G3": "rerun:2",
                             "G4": "rerun:intersection{run_reconcile}",
                             "G7": "final-head"}  # fmt: skip
    apart = reverify_plan(tmp_path, "old", "new", "fix2", graph=graph)
    assert not apart.rerun_g4
    assert apart.record()["G4"] == "reuse:no-intersection"
    assert isinstance(apart, Reverify)


# ---- the held disposition (A-47) ------------------------------------------------------------


STAGE4_SEEN_HITS = """CREATE TABLE seen_hits (
  hit_key TEXT PRIMARY KEY, hit_id TEXT, detection_id TEXT NOT NULL, trace_id TEXT NOT NULL,
  span_id TEXT NOT NULL, source TEXT NOT NULL CHECK (source IN ('webhook','poller','metric')),
  receptor TEXT, host TEXT, detected_at TEXT, seen_at REAL NOT NULL, incident TEXT,
  disposition TEXT CHECK (disposition IS NULL OR disposition IN ('opened','attached')));
CREATE INDEX seen_hits_detected ON seen_hits (detected_at);
INSERT INTO seen_hits VALUES ('k1', NULL, 'd1', 't1', 's1', 'poller', 'R-001', 'ledger',
  '2026-10-11T12:00:00Z', 1.0, 'INC-REH-0001', 'opened');"""


def test_a_stage4_store_migrates_and_held_hits_count_for_i7(tmp_path: Path) -> None:
    path = tmp_path / "senses.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(STAGE4_SEEN_HITS)
    store = SenseStore(path)
    kept = store.seen("k1")
    assert kept is not None
    assert (kept.incident, kept.disposition) == ("INC-REH-0001", "opened")
    hit = HitEvent(hit_key="k2", detection_id="d1", trace_id="t2", span_id="s2",
                   source="poller", receptor="R-001", host="ledger",
                   detected_at="2026-10-11T12:01:00Z")  # fmt: skip
    assert store.claim_hit(hit)
    store.assign("k2", "WATCH-R-001-ledger", "held")
    assert [h.hit_key for h in store.hits_for("R-001", "ledger")] == ["k1", "k2"]
    reported = [ReportedHit("d1", "t1", "s1", T0), ReportedHit("d1", "t2", "s2", T0)]
    keys = {r.key: r for r in reported}
    rows = {key: store.seen_hits()[k] for key, k in zip(keys, ("k1", "k2"), strict=True)}
    report = completeness(reported, rows, T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert report.ok
    assert (report.opened, report.held) == (1, 1)
    assert "1 held below threshold" in report.summary()
