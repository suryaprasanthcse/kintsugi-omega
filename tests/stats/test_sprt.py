from __future__ import annotations

import pytest

from kintsugi.stats.sprt import SPRT, Decision

CANARY = SPRT.regression(0.06)  # spec §5.12: p0 = 0.06, p1 = 0.12, α = 0.05, β = 0.10


def test_boundaries_match_the_spec() -> None:
    assert round(CANARY.upper, 3) == 2.890
    assert round(CANARY.lower, 3) == -2.251


def test_increments_match_the_spec() -> None:
    assert round(CANARY.failure_step, 4) == 0.6931
    assert round(CANARY.success_step, 4) == -0.0660


def test_zero_failures_accept_after_35_runs() -> None:
    assert CANARY.runs_to_accept_without_failures() == 35
    assert CANARY.run([False] * 34).decision is Decision.CONTINUE
    assert CANARY.run([False] * 35).decision is Decision.ACCEPT


def test_l3_scout_path_rejects_at_n8() -> None:
    # Spec §6.7: F S F F S F S F.
    outcomes = [True, False, True, True, False, True, False, True]
    result = CANARY.run(outcomes)
    assert result.decision is Decision.REJECT
    assert result.n == 8
    assert [round(x, 3) for x in result.path] == [
        0.693, 0.627, 1.320, 2.013, 1.948, 2.641, 2.575, 3.268,
    ]  # fmt: skip
    assert round(result.llr, 3) == 3.268


def test_l0_ledger_path_accepts_at_n46() -> None:
    # Spec §6.5 step 11: one failure in the first run, then 45 successes.
    result = CANARY.run([True] + [False] * 60)
    assert result.decision is Decision.ACCEPT
    assert result.n == 46
    assert round(result.llr, 3) == -2.275


def test_run_stops_at_the_first_boundary() -> None:
    result = CANARY.run([True] * 10)
    assert result.decision is Decision.REJECT
    assert result.n == 5  # 5 × 0.6931 = 3.466 ≥ 2.890; 4 × 0.6931 = 2.773 < 2.890


def test_unfinished_run_continues() -> None:
    result = CANARY.run([False, True, False])
    assert result.decision is Decision.CONTINUE
    assert result.n == 3


@pytest.mark.parametrize(
    "kwargs",
    [
        {"p0": 0.1, "p1": 0.05},
        {"p0": 0.0, "p1": 0.1},
        {"p0": 0.1, "p1": 1.0},
        {"p0": 0.1, "p1": 0.2, "alpha": 0.0},
    ],
)
def test_invalid_parameters_are_rejected(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError, match=r"need|must"):
        SPRT(**kwargs)
