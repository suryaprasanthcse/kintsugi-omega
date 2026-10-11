from __future__ import annotations

import numpy as np
import pytest

from kintsugi.econ import wright


def test_section_3_5_5_implied_learning_parameters() -> None:
    # E[C] = $10.28, $5.32, $2.84 at 80, 800, 8,000 cumulative incidents → b ≈ 0.28, PR ≈ 0.82.
    result = wright.fit([80, 800, 8000], [10.28, 5.32, 2.84], n_resamples=500)
    assert round(result.b, 2) == 0.28
    assert round(result.progress_ratio, 2) == 0.82
    assert round(result.learning_rate, 2) == 0.18


def test_noise_free_power_law_is_recovered_exactly() -> None:
    n = np.arange(1, 101)
    result = wright.fit(n, 14.0 * n**-0.28, n_resamples=200)
    assert result.b == pytest.approx(0.28, abs=1e-12)
    assert result.c1 == pytest.approx(14.0, rel=1e-12)
    assert result.r2 == pytest.approx(1.0)
    assert result.se_b == pytest.approx(0.0, abs=1e-12)


def test_noisy_power_law_confidence_intervals_cover_the_truth() -> None:
    rng = np.random.default_rng(2026)
    n = np.arange(1, 301)
    cost = 14.0 * n**-0.28 * rng.lognormal(0.0, 0.15, size=n.size)
    result = wright.fit(n, cost, seed=7)
    assert abs(result.b - 0.28) < 0.03
    assert result.ci_ols[0] < 0.28 < result.ci_ols[1]
    assert result.ci_bootstrap[0] < 0.28 < result.ci_bootstrap[1]
    assert result.progress_ratio == pytest.approx(2**-result.b)


def test_fit_is_reproducible_with_a_seed() -> None:
    rng = np.random.default_rng(1)
    n = np.arange(1, 50)
    cost = 10 * n**-0.3 * rng.lognormal(0, 0.2, n.size)
    assert wright.fit(n, cost, seed=3) == wright.fit(n, cost, seed=3)


def test_predict_follows_the_curve() -> None:
    assert wright.predict(1, 14.0, 0.28) == pytest.approx(14.0)
    assert wright.predict(2, 14.0, 0.28) == pytest.approx(14.0 * wright.progress_ratio(0.28))


@pytest.mark.parametrize(
    ("n", "cost", "message"),
    [
        ([1, 2], [3.0, 2.0], "three"),
        ([1, 2, 0], [3.0, 2.0, 1.0], "positive"),
        ([5, 5, 5], [3.0, 2.0, 1.0], "vary"),
    ],
)
def test_bad_inputs_are_rejected(n: list[int], cost: list[float], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        wright.fit(n, cost)


@pytest.mark.parametrize(
    ("m", "coverage_pct"), [(5, 45.8), (10, 57.0), (20, 67.9), (50, 81.5), (100, 91.0)]
)
def test_section_3_5_3_zipf_coverage(m: int, coverage_pct: float) -> None:
    assert round(100 * wright.zipf_coverage(m, 200, 1.1), 1) == coverage_pct


def test_zipf_coverage_bounds() -> None:
    assert wright.zipf_coverage(0, 200, 1.1) == 0.0
    assert wright.zipf_coverage(200, 200, 1.1) == pytest.approx(1.0)
    with pytest.raises(ValueError, match="m"):
        wright.zipf_coverage(201, 200, 1.1)
