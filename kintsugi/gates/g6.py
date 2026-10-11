"""G6, budget (spec §5.11, §4.6.3; invariant I4): cumulative incident spend ≤ B_ι.

Spend is the incident's cost log (every crew model call, every Surgeon session, every metered
eval), read through the governor's row of the information-flow matrix, never the Auditor's: the
Auditor does not see budgets (§5.7). A round that would start past the budget does not start;
the Coordinator then picks a cheaper treatment or escalates (L5).
"""

from __future__ import annotations

from kintsugi.gates.common import GateResult, failed, passed


def within_budget(spent_usd: float, budget_usd: float) -> GateResult:
    evidence = {"spent_usd": round(spent_usd, 4), "budget_usd": round(budget_usd, 4)}
    if spent_usd <= budget_usd + 1e-9:
        return passed("G6", f"${spent_usd:.2f} of ${budget_usd:.2f}", **evidence)
    return failed("G6", f"${spent_usd:.2f} exceeds the ${budget_usd:.2f} budget", **evidence)
