from __future__ import annotations

import numpy as np
import pytest

from kintsugi.stats.bootstrap import COST_MARGIN, P95_MARGIN, relative_delta


def _paired(seed: int = 7, n: int = 120) -> np.ndarray:
    return np.random.default_rng(seed).lognormal(mean=0.0, sigma=0.3, size=n) + 1.0


def test_margins_match_the_spec() -> None:
    assert (COST_MARGIN, P95_MARGIN) == (0.05, 0.10)


def test_identical_samples_have_zero_change() -> None:
    before = _paired()
    delta = relative_delta(before, before)
    assert delta.point == 0.0
    assert delta.upper == 0.0
    assert delta.non_inferior(COST_MARGIN)


def test_a_uniform_20_percent_increase_is_exact_and_fails() -> None:
    before = _paired()
    delta = relative_delta(before, before * 1.2)
    assert delta.point == pytest.approx(0.2)
    assert delta.upper == pytest.approx(0.2)
    assert not delta.non_inferior(COST_MARGIN)


def test_h3_p95_regression_fails_the_latency_gate() -> None:
    # Spec §6.3: H3's extra lookup per line pushed the p95 Δ upper bound to about +14%.
    rng = np.random.default_rng(11)
    before = rng.gamma(shape=4.0, scale=3.0, size=120)
    after = before * 1.14 + rng.normal(0, 0.05, size=120)
    delta = relative_delta(before, after, statistic="p95")
    assert delta.upper > P95_MARGIN
    assert not delta.non_inferior(P95_MARGIN)


def test_a_cheaper_fix_passes_the_cost_gate() -> None:
    # Spec §6.3: H2′ changed cost by −0.4%.
    rng = np.random.default_rng(3)
    before = rng.gamma(shape=6.0, scale=0.02, size=120)
    after = before * 0.996 + rng.normal(0, 0.0005, size=120)
    delta = relative_delta(before, after)
    assert delta.point < 0
    assert delta.non_inferior(COST_MARGIN)


def test_resampling_is_seeded_and_reproducible() -> None:
    rng = np.random.default_rng(5)
    before = rng.gamma(4.0, 2.0, 80)
    after = before + rng.normal(0.1, 0.5, 80)
    first = relative_delta(before, after, seed=42)
    assert relative_delta(before, after, seed=42) == first
    assert relative_delta(before, after, seed=43).upper != first.upper


@pytest.mark.parametrize(
    ("before", "after"),
    [([1.0, 2.0], [1.0]), ([], []), ([0.0, 0.0], [1.0, 1.0])],
)
def test_invalid_samples_are_rejected(before: list[float], after: list[float]) -> None:
    with pytest.raises(ValueError, match=r"paired|positive"):
        relative_delta(before, after)
