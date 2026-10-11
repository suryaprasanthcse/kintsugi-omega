"""Holm–Bonferroni step-down correction across a vaccination wave (spec §5.12).

Sort the p-values ascending and reject p_(i) while p_(i) ≤ α / (m − i + 1), i = 1…m.
"""

from __future__ import annotations

from collections.abc import Sequence


def holm(pvalues: Sequence[float], alpha: float = 0.05) -> list[bool]:
    """Reject decisions, in the original order of ``pvalues``."""
    m = len(pvalues)
    reject = [False] * m
    for rank, index in enumerate(sorted(range(m), key=lambda i: pvalues[i])):
        if pvalues[index] <= alpha / (m - rank):
            reject[index] = True
        else:
            break
    return reject


def holm_adjusted(pvalues: Sequence[float]) -> list[float]:
    """Holm-adjusted p-values (monotone), in the original order; reject where ≤ α."""
    m = len(pvalues)
    adjusted = [0.0] * m
    running = 0.0
    for rank, index in enumerate(sorted(range(m), key=lambda i: pvalues[i])):
        running = max(running, min(1.0, (m - rank) * pvalues[index]))
        adjusted[index] = running
    return adjusted
