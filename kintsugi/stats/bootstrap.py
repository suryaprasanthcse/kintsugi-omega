"""Non-inferiority of cost and latency for gate G4 (spec §5.12).

The one-sided 95% bootstrap upper bound of (stat_after − stat_before) / stat_before must be
below δ: δ_cost = 0.05 for mean cost, δ_p95 = 0.10 for p95 latency. Samples are paired by
holdout task × seed, so pairs are resampled together. Resampling is seeded: every gate verdict
can be reproduced exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.typing import ArrayLike, NDArray

COST_MARGIN = 0.05
P95_MARGIN = 0.10
Statistic = Literal["mean", "p95"]


@dataclass(frozen=True)
class RelativeDelta:
    point: float
    upper: float
    confidence: float
    statistic: Statistic

    def non_inferior(self, margin: float) -> bool:
        return self.upper < margin


def _stat(samples: NDArray[np.float64], statistic: Statistic) -> NDArray[np.float64]:
    if statistic == "mean":
        return samples.mean(axis=-1)
    return np.percentile(samples, 95, axis=-1)


def relative_delta(
    before: ArrayLike,
    after: ArrayLike,
    *,
    statistic: Statistic = "mean",
    confidence: float = 0.95,
    n_resamples: int = 10_000,
    seed: int = 0,
) -> RelativeDelta:
    """Point estimate and one-sided bootstrap upper bound of the paired relative change."""
    b = np.asarray(before, dtype=np.float64)
    a = np.asarray(after, dtype=np.float64)
    if b.shape != a.shape or b.ndim != 1 or b.size == 0:
        raise ValueError("before and after must be equal-length, non-empty, paired samples")
    base = float(_stat(b, statistic))
    if base <= 0.0:
        raise ValueError("the baseline statistic must be positive")
    point = (float(_stat(a, statistic)) - base) / base
    rng = np.random.default_rng(seed)
    index = rng.integers(0, b.size, size=(n_resamples, b.size))
    base_resampled = _stat(b[index], statistic)
    ratios = (_stat(a[index], statistic) - base_resampled) / base_resampled
    upper = float(np.quantile(ratios, confidence))
    return RelativeDelta(point=point, upper=upper, confidence=confidence, statistic=statistic)
