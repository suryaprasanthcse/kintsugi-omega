"""Budget governor (spec §4.6.2–§4.6.3, §6.8). Pure functions; Stage 9 feeds them live actuals.

    Rev_h       = P_tier + F_prev · prevented_h
    Pool_h      = Rev_h · (1 − GM_floor) − C_mon,h        (repair budget per host per month)
    Remaining_h = Pool_h − spent_h(month-to-date)
    Cap_ι       = Remaining_h / E[remaining incidents this month]
    VaR_ι       = r_fail · λ_h · v_h · T_horizon           (dollars at risk if nothing is done)
    B_ι         = min(Cap_ι, κ · VaR_ι)
    Priority(ι) = VaR_ι / E[C_ι]

Repairs are sized by power analysis and never by lowering power (``stats.power.POWER_FLOOR``):
when the required sample does not fit the budget the governor first reduces the fork count K,
then turns to a cheaper treatment (L5), and otherwise escalates.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

GM_FLOOR_DEFAULT = 0.70
K_OPTIONS = (4, 3, 2, 1)


class BudgetExceeded(RuntimeError):
    """A charge would breach a hard cap (invariant I4)."""


def revenue(price: float, outcome_fee: float = 0.0, prevented: int = 0) -> float:
    return price + outcome_fee * prevented


def pool(rev: float, gm_floor: float, c_mon: float) -> float:
    return rev * (1.0 - gm_floor) - c_mon


def remaining(pool_usd: float, spent_mtd: float) -> float:
    return pool_usd - spent_mtd


def cap(remaining_usd: float, expected_remaining_incidents: float) -> float:
    if expected_remaining_incidents <= 0:
        raise ValueError("expected remaining incidents must be positive")
    return max(0.0, remaining_usd) / expected_remaining_incidents


def value_at_risk(r_fail: float, lam_per_h: float, v_per_task: float, horizon_h: float) -> float:
    return r_fail * lam_per_h * v_per_task * horizon_h


def budget(cap_usd: float, var_usd: float, kappa: float = 1.0) -> float:
    return max(0.0, min(cap_usd, kappa * var_usd))


def priority(var_usd: float, expected_cost: float) -> float:
    if expected_cost <= 0:
        raise ValueError("expected cost must be positive")
    return var_usd / expected_cost


def gross_margin(rev: float, cogs: float) -> float:
    if rev <= 0:
        raise ValueError("revenue must be positive")
    return (rev - cogs) / rev


def expected_incident_cost(q: float, c_secondary: float, c_primary: float) -> float:
    """E[C_ι] = q·c_s + (1 − q)·c_p (spec §3.5.2)."""
    if not 0.0 <= q <= 1.0:
        raise ValueError("coverage q must be in [0, 1]")
    return q * c_secondary + (1.0 - q) * c_primary


@dataclass(frozen=True)
class IncidentBudget:
    pool: float
    remaining: float
    cap: float
    var: float
    budget: float


def incident_budget(
    *,
    price: float,
    gm_floor: float,
    c_mon: float,
    spent_mtd: float,
    expected_remaining_incidents: float,
    r_fail: float,
    lam_per_h: float,
    v_per_task: float,
    horizon_h: float = 24.0,
    kappa: float = 1.0,
    outcome_fee: float = 0.0,
    prevented: int = 0,
) -> IncidentBudget:
    p = pool(revenue(price, outcome_fee, prevented), gm_floor, c_mon)
    r = remaining(p, spent_mtd)
    c = cap(r, expected_remaining_incidents)
    v = value_at_risk(r_fail, lam_per_h, v_per_task, horizon_h)
    return IncidentBudget(pool=p, remaining=r, cap=c, var=v, budget=budget(c, v, kappa))


@dataclass(frozen=True)
class RepairPlan:
    k: int
    n_pairs: int
    cost: float


def repair_cost(n_pairs: int, cost_per_run: float, k: int, cost_per_hypothesis: float) -> float:
    return n_pairs * cost_per_run + k * cost_per_hypothesis


def plan_repair(
    n_required: int,
    cost_per_run: float,
    cost_per_hypothesis: float,
    budget_usd: float,
    k_options: Sequence[int] = K_OPTIONS,
) -> RepairPlan | None:
    """The largest fork count whose full-power repair fits the budget, or ``None``."""
    for k in sorted(k_options, reverse=True):
        cost = repair_cost(n_required, cost_per_run, k, cost_per_hypothesis)
        if cost <= budget_usd + 1e-9:
            return RepairPlan(k=k, n_pairs=n_required, cost=cost)
    return None


@dataclass(frozen=True)
class Treatment:
    """A cheaper intervention than repair, e.g. routing a step to a stronger model (L5)."""

    name: str
    probe_cost: float
    delta_cost_per_month: float


def treatment_cost_this_month(
    treatment: Treatment, days_remaining: float, days_in_month: float = 30.0
) -> float:
    return treatment.probe_cost + treatment.delta_cost_per_month * days_remaining / days_in_month


class Action(StrEnum):
    REPAIR = "repair"
    TREAT_AND_ESCALATE = "treat_and_escalate"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class EconomicDecision:
    """Everything the L5 decision span records: every alternative, its cost, the choice."""

    action: Action
    budget: float
    plan: RepairPlan | None
    treatment: Treatment | None
    alternatives: dict[str, float] = field(default_factory=dict)
    rejected: dict[str, str] = field(default_factory=dict)


def decide(
    *,
    budget_usd: float,
    n_required: int,
    cost_per_run: float,
    cost_per_hypothesis: float,
    treatments: Sequence[Treatment],
    days_remaining: float,
    price: float,
    gm_floor: float,
    c_mon: float,
    do_nothing_loss_per_day: float,
    days_in_month: float = 30.0,
    k_options: Sequence[int] = K_OPTIONS,
) -> EconomicDecision:
    alternatives: dict[str, float] = {}
    rejected: dict[str, str] = {}
    for k in sorted(k_options, reverse=True):
        cost = repair_cost(n_required, cost_per_run, k, cost_per_hypothesis)
        alternatives[f"repair K={k}"] = cost
        if cost > budget_usd + 1e-9:
            rejected[f"repair K={k}"] = "exceeds budget"
    rejected["repair with lower n"] = "power below 0.8 is forbidden by policy"
    alternatives["do nothing (per day)"] = do_nothing_loss_per_day
    rejected["do nothing (per day)"] = "expected loss continues"

    plan = plan_repair(n_required, cost_per_run, cost_per_hypothesis, budget_usd, k_options)
    if plan is not None:
        return EconomicDecision(Action.REPAIR, budget_usd, plan, None, alternatives, rejected)

    affordable: list[tuple[float, Treatment]] = []
    for treatment in treatments:
        cost = treatment_cost_this_month(treatment, days_remaining, days_in_month)
        alternatives[treatment.name] = cost
        margin_pool = pool(price, gm_floor, c_mon + treatment.delta_cost_per_month)
        if cost > budget_usd + 1e-9:
            rejected[treatment.name] = "exceeds budget"
        elif margin_pool <= 0:
            rejected[treatment.name] = "breaks the margin floor"
        else:
            affordable.append((cost, treatment))
    if affordable:
        _, chosen = min(affordable, key=lambda item: item[0])
        return EconomicDecision(
            Action.TREAT_AND_ESCALATE, budget_usd, None, chosen, alternatives, rejected
        )
    return EconomicDecision(Action.ESCALATE, budget_usd, None, None, alternatives, rejected)


class DailyCap:
    """A hard daily dollar cap (ruling D7). Stage 3 persists it; the proxy and governor share it."""

    def __init__(self, cap_usd: float) -> None:
        if cap_usd <= 0:
            raise ValueError("the daily cap must be positive")
        self.cap_usd = cap_usd
        self._spent: dict[str, float] = {}

    def spent(self, day: str) -> float:
        return self._spent.get(day, 0.0)

    def headroom(self, day: str) -> float:
        return self.cap_usd - self.spent(day)

    def charge(self, day: str, amount_usd: float) -> float:
        """Record a charge for ``day`` (YYYY-MM-DD); refuse it whole if it would breach the cap."""
        if amount_usd < 0:
            raise ValueError("charges must be non-negative")
        if self.spent(day) + amount_usd > self.cap_usd + 1e-9:
            raise BudgetExceeded(
                f"{day}: ${amount_usd:.2f} would exceed the ${self.cap_usd:.2f} daily cap "
                f"(spent ${self.spent(day):.2f})"
            )
        self._spent[day] = self.spent(day) + amount_usd
        return self.headroom(day)
