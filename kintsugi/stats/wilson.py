"""Wilson score interval for pass rates (spec §5.12).

Formula: (p̂ + z²/2n ± z·√(p̂(1−p̂)/n + z²/4n²)) / (1 + z²/n). At k = 0 and k = n the bound
that equals 0 or 1 analytically is pinned exactly, so floating-point error never reports
0.9999999999999999 for a perfect pass rate.
"""

from __future__ import annotations

import math

from scipy.stats import norm


def wilson_interval(successes: int, n: int, confidence: float = 0.95) -> tuple[float, float]:
    if n <= 0:
        raise ValueError("n must be positive")
    if not 0 <= successes <= n:
        raise ValueError("successes must be between 0 and n")
    z = float(norm.ppf(1.0 - (1.0 - confidence) / 2.0))
    p = successes / n
    denominator = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / denominator
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / denominator
    low = 0.0 if successes == 0 else max(0.0, centre - half)
    high = 1.0 if successes == n else min(1.0, centre + half)
    return low, high
