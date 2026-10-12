"""Activity contracts (spec §5.3.2): timeouts, heartbeats, retries, idempotency keys, pools."""

from __future__ import annotations

import asyncio
import inspect
import time
from datetime import timedelta
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from kintsugi.control import activities as a
from kintsugi.control import contracts as c

M, S = timedelta(minutes=1), timedelta(seconds=1)

# Spec §5.3.2, verbatim: activity → (start_to_close, heartbeat, attempts, non-retryable).
SPEC_TABLE = {
    "triage": (5 * M, None, 3, ("InvalidAlert",)),
    "reproduce": (15 * M, 60 * S, 3, ()),
    "attribute": (20 * M, 60 * S, 2, ()),
    "plan_budget": (2 * M, None, 3, ()),
    "surgeon": (45 * M, 60 * S, 2, ("BudgetExceeded", "ProtectedPathViolation")),
    "audit_gates": (60 * M, 60 * S, 2, ()),
    "coordinate": (5 * M, None, 3, ()),
    "dispute_experiment": (30 * M, 60 * S, 1, ()),
    "ship": (10 * M, None, 5, ()),
    "revert": (10 * M, None, 5, ()),
    "synthesize_antibody": (20 * M, 60 * S, 2, ()),
    "ledger_append": (1 * M, None, 10, ()),
}

SIG = c.Signature("INC-0042", "ledger", "phi-1", ("t1",))
CARD = c.Card("INC-0042", "H1", 0, "hoist the session", "base")


@pytest.mark.parametrize("name", sorted(SPEC_TABLE))
def test_each_spec_row_is_encoded_exactly(name: str) -> None:
    spec = a.SPECS[name]
    timeout, heartbeat, attempts, non_retryable = SPEC_TABLE[name]
    assert (spec.start_to_close, spec.heartbeat) == (timeout, heartbeat)
    assert spec.max_attempts == attempts
    assert spec.non_retryable == non_retryable
    policy = spec.retry_policy()
    assert policy.maximum_attempts == attempts
    assert list(policy.non_retryable_error_types or []) == list(non_retryable)


def test_every_spec_has_an_activity_and_every_activity_a_spec() -> None:
    acts = a.OmegaActivities(a.ScriptedCrew({}))
    methods = {
        name for name, fn in inspect.getmembers(acts, inspect.ismethod)
        if hasattr(fn, "__temporal_activity_definition")
    }  # fmt: skip
    assert methods == set(a.SPECS)
    assert {a.SURGEON} == {s.task_queue for s in a.specs_for(a.SURGEON)}
    assert a.SPECS["surgeon"].task_queue == a.SURGEON  # its own pool, the kill -9 target (§6.6)
    assert a.SPECS["tag_traces"].task_queue == a.SENSES
    by_queue = sum(len(acts.activities_for(q)) for q in a.TASK_QUEUES)
    assert by_queue == len(a.SPECS)


def test_idempotency_keys_follow_the_spec_table() -> None:
    spec = a.SPECS["surgeon"]
    assert spec.idempotency_key(CARD) == spec.idempotency_key(CARD)
    assert spec.idempotency_key(CARD) != spec.idempotency_key(c.Card("INC-0042", "H2", 0, "", ""))
    assert spec.idempotency_key(CARD) != spec.idempotency_key(c.Card("INC-0042", "H1", 1, "", ""))
    alert = c.Alert("INC-0042", "ledger", ())
    triage = a.SPECS["triage"]
    assert triage.idempotency_key(alert) != triage.idempotency_key(alert.with_evidence("canary"))
    out = c.SurgeonOutcome("INC-0042", "H1", 1, "kintsugi/INC-0042/H1", "nonce", "abc")
    other = c.SurgeonOutcome("INC-0042", "H1", 2, "kintsugi/INC-0042/H1", "n2", "def")
    ship = a.SPECS["ship"]
    assert ship.idempotency_key(out) == ship.idempotency_key(other)  # keyed by the branch name
    rec = a.SPECS["record_transition"]
    t1 = c.TransitionRecord("INC-0042", 4, "REPAIRING", "REPAIRING", "round_failed")
    t2 = c.TransitionRecord("INC-0042", 5, "REPAIRING", "REPAIRING", "round_failed")
    assert rec.idempotency_key(t1) != rec.idempotency_key(t2)
    reentered = c.Signature("INC-0042", "ledger", "phi-1", ("t1",), pass_no=1)
    assert a.SPECS["reproduce"].idempotency_key(SIG) != a.SPECS["reproduce"].idempotency_key(
        reentered
    )
    # A fleet scan's answer changes as hosts get protected: the maturation's scan must not be
    # served to the wave (found by the Stage 7 rehearsal l3-20261012-074523).
    scan, ab = a.SPECS["motif_scan"], c.AntibodyRef("AB-1", "1.0")
    assert scan.idempotency_key(ab, workflow_id="mature-AB-1") == scan.idempotency_key(
        ab, workflow_id="mature-AB-1"
    )
    assert scan.idempotency_key(ab, workflow_id="mature-AB-1") != scan.idempotency_key(
        ab, workflow_id="vaccinate-AB-1"
    )
    assert a.SPECS["surgeon"].idempotency_key(CARD, workflow_id="x") == spec.idempotency_key(CARD)


def test_a_replayed_step_returns_its_recorded_result_without_redoing_it() -> None:
    crew = a.ScriptedCrew({"reproduce": lambda sig: c.Repro(True, "cassette-1", 3, 3)})
    acts = a.OmegaActivities(crew)
    env = ActivityEnvironment()
    first = asyncio.run(env.run(acts.reproduce, SIG))
    second = asyncio.run(env.run(acts.reproduce, SIG))
    assert first == second == c.Repro(True, "cassette-1", 3, 3)
    assert crew.count("reproduce") == 1


def test_long_activities_heartbeat_while_the_crew_works() -> None:
    def slow(card: c.Card) -> c.SurgeonOutcome:
        time.sleep(0.5)
        return c.SurgeonOutcome(card.incident_id, card.hypothesis, 1, "b", "n", "sha")

    acts = a.OmegaActivities(a.ScriptedCrew({"surgeon": slow}), heartbeat_interval_s=0.1)
    env = ActivityEnvironment()
    beats: list[tuple[object, ...]] = []
    env.on_heartbeat = lambda *details: beats.append(details)
    result = asyncio.run(env.run(acts.surgeon, CARD))
    assert result.commit_sha == "sha"
    assert len(beats) >= 3


def test_budget_and_protected_path_errors_are_non_retryable() -> None:
    def refuse(card: c.Card) -> c.SurgeonOutcome:
        raise a.BudgetExceeded("the incident's budget is spent")

    acts = a.OmegaActivities(a.ScriptedCrew({"surgeon": refuse}), heartbeat_interval_s=0.05)
    with pytest.raises(ApplicationError) as caught:
        asyncio.run(ActivityEnvironment().run(acts.surgeon, CARD))
    assert caught.value.non_retryable
    assert caught.value.type == "BudgetExceeded"


def test_the_sqlite_ledger_is_shared_across_workers(tmp_path: Path) -> None:
    path = tmp_path / "steps.sqlite"
    value = c.SurgeonOutcome("INC-0042", "H1", 2, "b", "n", None, abandoned=("attempt 1",))
    a.SqliteLedger(path).put("k", a.encode(value))
    a.SqliteLedger(path).put("k", a.encode("ignored: first write wins"))
    assert a.decode(a.SqliteLedger(path).get("k") or b"", c.SurgeonOutcome) == value
    assert a.SqliteLedger(path).get("missing") is None
    assert a.decode(a.encode(None), c.AntibodyMatch | None) is None


def test_outcomes_must_be_terminal() -> None:
    assert c.Outcome("INC-1", c.RESOLVED, "canary accepted").state == "RESOLVED"
    with pytest.raises(ValueError, match="RESOLVED, ESCALATED or PARKED"):
        c.Outcome("INC-1", "REPAIRING", "still working")
