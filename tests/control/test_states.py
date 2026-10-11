"""Invariant I3: every incident terminates in RESOLVED, ESCALATED or PARKED."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kintsugi.control import states as s
from kintsugi.control.states import IncidentState as S


def test_the_spec_table_alone_leaves_four_ways_to_get_stuck() -> None:
    assert s.missing_exits(spec_only=True) == [
        (S.ATTRIBUTED, "budget_zero"),
        (S.CANARY, "sprt_rejects_revert_failed_or_reentry_limit"),
        (S.REPRODUCED, "attribution_found_no_suspect"),
        (S.SHIPPED, "merge_blocked"),
    ]


def test_with_the_closure_transitions_every_outcome_has_an_exit() -> None:
    assert s.missing_exits() == []


def test_spec_only_model_does_not_terminate() -> None:
    report = s.check_termination(s.Bounds(), spec_only=True)
    assert not report.every_path_terminates
    stuck_states = {state for state, _, _ in report.stuck}
    assert {S.REPRODUCED, S.ATTRIBUTED, S.SHIPPED, S.CANARY} <= stuck_states


def test_default_bounds_terminate_exhaustively() -> None:
    report = s.check_termination(s.Bounds(max_rounds=3, max_canary_reentries=2))
    assert report.every_path_terminates
    assert report.acyclic
    assert report.stuck == ()
    assert report.longest_path > 0


@given(st.integers(min_value=1, max_value=6), st.integers(min_value=0, max_value=4))
def test_every_bound_choice_terminates(max_rounds: int, reentries: int) -> None:
    assert s.check_termination(s.Bounds(max_rounds, reentries)).every_path_terminates


@given(
    st.integers(min_value=1, max_value=5),
    st.integers(min_value=0, max_value=3),
    st.data(),
)
def test_random_walks_reach_a_terminal_state(
    max_rounds: int, reentries: int, data: st.DataObject
) -> None:
    bounds = s.Bounds(max_rounds, reentries)
    limit = s.check_termination(bounds).longest_path
    current: s.ModelState = (S.OPEN, 0, 0)
    steps = 0
    while current[0] not in s.TERMINAL:
        outcome = data.draw(st.sampled_from(sorted(s.available_outcomes(current, bounds))))
        successor = s.step(current, outcome)
        assert successor is not None
        current = successor
        steps += 1
        assert steps <= limit
    assert current[0] in s.TERMINAL


def test_transitions_are_consistent_with_the_outcome_model() -> None:
    for transition in s.TRANSITIONS:
        assert transition.source not in s.TERMINAL
        assert transition.outcome in s.OUTCOMES[transition.source]
    assert set(s.OUTCOMES) == set(S) - s.TERMINAL


def test_fold_replays_the_l0_and_l1_path() -> None:
    # Spec §6.2–§6.5: reproduce, attribute, round 1 fails, round 2 ships, canary accepts.
    log = [
        (S.OPEN, S.REPRODUCED, "g0_fails_3_of_3"),
        (S.REPRODUCED, S.ATTRIBUTED, "cause_report_has_suspect"),
        (S.ATTRIBUTED, S.REPAIRING, "budget_positive"),
        (S.REPAIRING, S.REPAIRING, "round_failed_rounds_and_budget_remain"),
        (S.REPAIRING, S.SHIPPED, "winner_passed_g0_g6_pr_open_g7_green"),
        (S.SHIPPED, S.CANARY, "merged_or_pr_opened"),
        (S.CANARY, S.RESOLVED, "sprt_accepts_h0_or_pr_only_canary_skipped"),
    ]
    assert s.fold(log) is S.RESOLVED


def test_fold_replays_the_l3_reentry() -> None:
    log = [
        (S.OPEN, S.REPRODUCED, "g0_fails_3_of_3"),
        (S.REPRODUCED, S.ATTRIBUTED, "cause_report_has_suspect"),
        (S.ATTRIBUTED, S.REPAIRING, "budget_positive"),
        (S.REPAIRING, S.SHIPPED, "winner_passed_g0_g6_pr_open_g7_green"),
        (S.SHIPPED, S.CANARY, "merged_or_pr_opened"),
        (S.CANARY, S.REPAIRING, "sprt_rejects_reverted_and_quarantined"),
        (S.REPAIRING, S.ESCALATED, "rounds_or_budget_exhausted_or_cheaper_treatment"),
    ]
    assert s.fold(log) is S.ESCALATED


def test_fold_rejects_a_skipped_state() -> None:
    with pytest.raises(s.TransitionError, match="not a legal transition"):
        s.fold([(S.OPEN, S.ATTRIBUTED, "g0_fails_3_of_3")])


def test_fold_rejects_an_inconsistent_log() -> None:
    with pytest.raises(s.TransitionError, match="but the incident is OPEN"):
        s.fold([(S.REPRODUCED, S.ATTRIBUTED, "cause_report_has_suspect")])


def test_unknown_outcomes_are_rejected() -> None:
    with pytest.raises(s.TransitionError, match="no transition"):
        s.next_state(S.CANARY, "vibes")


def test_bounds_are_validated() -> None:
    with pytest.raises(ValueError, match="max_rounds"):
        s.Bounds(max_rounds=0)
