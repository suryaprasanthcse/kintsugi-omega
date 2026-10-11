"""Sample size for paired evaluations: Connor's formula for McNemar (spec §5.12).

    n = [ z_{1−α}·√ψ + z_{1−β}·√(ψ − δ²) ]² / δ²

ψ is the expected discordant proportion (p10 + p01) and δ the expected difference (p10 − p01).
Policy (spec §4.6.3): power is never lowered below 0.8. When n does not fit the budget, the
governor reduces K or escalates; it does not trade away power.
"""

from __future__ import annotations

import math

from scipy.stats import norm

POWER_FLOOR = 0.80


def discordant(p10: float, p01: float) -> tuple[float, float]:
    """(ψ, δ) from the two discordant-cell probabilities."""
    return p10 + p01, p10 - p01


def connor_n(psi: float, delta: float, *, alpha: float = 0.05, power: float = POWER_FLOOR) -> int:
    """Paired runs needed for a one-sided McNemar test at level ``alpha`` with ``power``."""
    if power < POWER_FLOOR:
        raise ValueError(f"power {power} is below the policy floor {POWER_FLOOR}")
    if not 0.0 < power < 1.0 or not 0.0 < alpha < 1.0:
        raise ValueError("alpha and power must be in (0, 1)")
    if delta <= 0.0 or psi > 1.0 or psi - delta**2 <= 0.0:
        raise ValueError("need δ > 0, ψ ≤ 1 and ψ > δ²")
    z_alpha = float(norm.ppf(1.0 - alpha))
    z_beta = float(norm.ppf(power))
    n = (z_alpha * math.sqrt(psi) + z_beta * math.sqrt(psi - delta**2)) ** 2 / delta**2
    return math.ceil(n)
