from __future__ import annotations

import pytest

from kintsugi.econ import governor as g


def test_section_6_2_ledger_incident_budget() -> None:
    # Ledger, Team tier: P = $299, GM_floor = 0.70, C_mon = $18, spent MTD = $22.10,
    # E[remaining incidents] = 4; VaR_24h = 0.18 × 40/h × $3 × 24 h.
    b = g.incident_budget(
        price=299, gm_floor=0.70, c_mon=18, spent_mtd=22.10, expected_remaining_incidents=4,
        r_fail=0.18, lam_per_h=40, v_per_task=3.00, horizon_h=24,
    )  # fmt: skip
    assert round(b.pool, 2) == 71.70
    assert round(b.remaining, 2) == 49.60
    assert round(b.cap, 2) == 12.40
    assert round(b.var) == 518
    assert round(b.budget, 2) == 12.40


def test_section_6_8_triage_incident_budget() -> None:
    b = g.incident_budget(
        price=99, gm_floor=0.70, c_mon=6.00, spent_mtd=9.20, expected_remaining_incidents=3,
        r_fail=0.15, lam_per_h=1.25, v_per_task=1.50, horizon_h=24,
    )  # fmt: skip
    assert round(b.var, 2) == 6.75
    assert round(b.pool, 2) == 23.70
    assert round(b.remaining, 2) == 14.50
    assert round(b.cap, 2) == 4.83
    assert round(b.budget, 2) == 4.83


T1 = g.Treatment(name="T1 route classify_component Haiku→Sonnet", probe_cost=0.90,
                 delta_cost_per_month=3.60)  # fmt: skip


def test_section_6_8_l5_decision_treats_and_escalates() -> None:
    decision = g.decide(
        budget_usd=4.83, n_required=60, cost_per_run=0.11, cost_per_hypothesis=1.40,
        treatments=[T1], days_remaining=12, price=99, gm_floor=0.70, c_mon=6.00,
        do_nothing_loss_per_day=6.75,
    )  # fmt: skip
    assert decision.action is g.Action.TREAT_AND_ESCALATE
    assert decision.treatment == T1
    assert round(decision.alternatives["repair K=3"], 2) == 10.80
    assert round(decision.alternatives["repair K=1"], 2) == 8.00
    assert round(decision.alternatives[T1.name], 2) == 2.34
    assert decision.rejected["repair K=3"] == "exceeds budget"
    assert decision.rejected["repair with lower n"] == "power below 0.8 is forbidden by policy"
    # Pool' = 99 × 0.30 − (6.00 + 3.60) = $20.10 > 0: the margin floor stays achievable.
    assert round(g.pool(99, 0.70, 6.00 + 3.60), 2) == 20.10


def test_section_6_8_month_gross_margin_after_treatment() -> None:
    cogs = 6.00 + 3.60 + 9.20 + 2.34
    assert round(cogs, 2) == 21.14
    assert round(100 * g.gross_margin(99, cogs), 1) == 78.6
    assert g.gross_margin(99, cogs) >= g.GM_FLOOR_DEFAULT


def test_repair_is_chosen_when_it_fits_with_the_largest_k() -> None:
    plan = g.plan_repair(n_required=34, cost_per_run=0.10, cost_per_hypothesis=1.0, budget_usd=7.0)
    assert plan is not None
    assert (plan.k, plan.n_pairs) == (3, 34)
    assert plan.cost == pytest.approx(6.4)


def test_escalate_when_nothing_fits() -> None:
    expensive = g.Treatment(name="T2", probe_cost=10.0, delta_cost_per_month=1.0)
    decision = g.decide(
        budget_usd=1.0, n_required=60, cost_per_run=0.11, cost_per_hypothesis=1.40,
        treatments=[expensive], days_remaining=12, price=99, gm_floor=0.70, c_mon=6.00,
        do_nothing_loss_per_day=6.75,
    )  # fmt: skip
    assert decision.action is g.Action.ESCALATE
    assert decision.rejected["T2"] == "exceeds budget"


def test_a_treatment_that_breaks_the_margin_floor_is_rejected() -> None:
    margin_breaker = g.Treatment(name="T3", probe_cost=0.10, delta_cost_per_month=40.0)
    decision = g.decide(
        budget_usd=50.0, n_required=600, cost_per_run=1.0, cost_per_hypothesis=1.0,
        treatments=[margin_breaker], days_remaining=1, price=99, gm_floor=0.70, c_mon=6.00,
        do_nothing_loss_per_day=1.0,
    )  # fmt: skip
    assert decision.action is g.Action.ESCALATE
    assert decision.rejected["T3"] == "breaks the margin floor"


@pytest.mark.parametrize(
    ("q", "e_cost", "incident_month", "cogs", "gm_pct"),
    [
        (0.30, 10.28, 82.24, 100.24, 66.5),
        (0.70, 5.32, 42.56, 60.56, 79.7),
        (0.90, 2.84, 22.72, 40.72, 86.4),
    ],
)
def test_section_3_5_5_unit_economics_table(
    q: float, e_cost: float, incident_month: float, cogs: float, gm_pct: float
) -> None:
    # P = $299, C_mon = $18, μ = 8 incidents/host/month, c_p = $14.00, c_s = $1.60.
    expected = g.expected_incident_cost(q, c_secondary=1.60, c_primary=14.00)
    assert round(expected, 2) == e_cost
    assert round(8 * expected, 2) == incident_month
    assert round(18 + 8 * expected, 2) == cogs
    assert round(100 * g.gross_margin(299, 18 + 8 * expected), 1) == gm_pct


def test_priority_is_value_per_repair_dollar() -> None:
    assert g.priority(518.4, 12.4) == pytest.approx(41.806, rel=1e-3)


def test_budget_never_goes_negative() -> None:
    assert g.budget(-5.0, 100.0) == 0.0
    assert g.cap(-20.0, 4) == 0.0


def test_daily_cap_refuses_a_breaching_charge_whole() -> None:
    cap = g.DailyCap(25.0)
    assert cap.charge("2026-10-12", 20.0) == pytest.approx(5.0)
    with pytest.raises(g.BudgetExceeded, match="daily cap"):
        cap.charge("2026-10-12", 5.01)
    assert cap.spent("2026-10-12") == pytest.approx(20.0)
    assert cap.charge("2026-10-13", 25.0) == pytest.approx(0.0)


def test_invalid_inputs_are_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        g.cap(10.0, 0)
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        g.expected_incident_cost(1.5, 1.0, 2.0)
    with pytest.raises(ValueError, match="positive"):
        g.DailyCap(0)
