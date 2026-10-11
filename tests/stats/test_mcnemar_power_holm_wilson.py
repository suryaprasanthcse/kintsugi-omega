from __future__ import annotations

import math

import pytest

from kintsugi.stats.holm import holm, holm_adjusted
from kintsugi.stats.mcnemar import discordant_counts, exact_one_sided_p, mcnemar
from kintsugi.stats.power import POWER_FLOOR, connor_n, discordant
from kintsugi.stats.wilson import wilson_interval

# ---- McNemar (G4) ---------------------------------------------------------------------------


def test_h2_prime_winner_p_value() -> None:
    # Spec §6.3 round 2: b = 26, c = 1 → p ≈ 2.1e-7. Exactly P(X ≥ 26 | n = 27) = 28 / 2^27.
    p = exact_one_sided_p(26, 1)
    assert p == pytest.approx(28 / 2**27, rel=1e-12)
    assert f"{p:.2e}" == "2.09e-07"


def test_h3_double_prime_p_value() -> None:
    assert exact_one_sided_p(25, 1) == pytest.approx(27 / 2**26, rel=1e-12)


def test_no_discordant_pairs_gives_no_evidence() -> None:
    assert exact_one_sided_p(0, 0) == 1.0


def test_discordant_counts_from_paired_outcomes() -> None:
    before = [False, False, True, True, False]
    after = [True, False, True, False, True]
    assert discordant_counts(before, after) == (2, 1)
    result = mcnemar(before, after)
    assert (result.b, result.c, result.pairs_discordant) == (2, 1, 3)


def test_unpaired_inputs_are_rejected() -> None:
    with pytest.raises(ValueError, match="paired"):
        discordant_counts([True], [True, False])


# ---- Connor sample size --------------------------------------------------------------------


def test_connor_worked_example_needs_34_pairs() -> None:
    psi, delta = discordant(0.25, 0.03)
    assert (round(psi, 2), round(delta, 2)) == (0.28, 0.22)
    assert connor_n(psi, delta, alpha=0.05, power=0.80) == 34


def test_the_spec_holdout_is_well_powered() -> None:
    assert connor_n(0.28, 0.22) <= 40 * 3


def test_power_is_never_lowered_below_the_floor() -> None:
    assert POWER_FLOOR == 0.80
    with pytest.raises(ValueError, match="floor"):
        connor_n(0.28, 0.22, power=0.70)


def test_higher_power_needs_more_pairs() -> None:
    assert connor_n(0.28, 0.22, power=0.90) > connor_n(0.28, 0.22, power=0.80)


@pytest.mark.parametrize(("psi", "delta"), [(0.28, 0.0), (0.04, 0.22), (1.2, 0.1)])
def test_connor_rejects_impossible_cells(psi: float, delta: float) -> None:
    with pytest.raises(ValueError, match="need"):
        connor_n(psi, delta)


# ---- Holm–Bonferroni ------------------------------------------------------------------------


def test_holm_step_down() -> None:
    # m = 4: 0.005 ≤ .0125 ✓, 0.01 ≤ .0167 ✓, 0.03 > .025 ✗ → stop.
    assert holm([0.01, 0.04, 0.03, 0.005]) == [True, False, False, True]


def test_holm_stops_at_the_first_failure() -> None:
    # m = 3: 0.001 ≤ .0167 ✓; 0.04 > .025 ✗ → stop, so 0.045 is kept although 0.045 ≤ .05.
    assert holm([0.001, 0.04, 0.045]) == [True, False, False]


def test_holm_rejects_everything_when_every_step_passes() -> None:
    assert holm([0.001, 0.02, 0.0125]) == [True, True, True]


def test_holm_adjusted_matches_decisions() -> None:
    pvalues = [0.01, 0.04, 0.03, 0.005]
    adjusted = holm_adjusted(pvalues)
    assert adjusted == pytest.approx([0.03, 0.06, 0.06, 0.02])
    assert [p <= 0.05 for p in adjusted] == holm(pvalues)


def test_holm_on_an_empty_wave() -> None:
    assert holm([]) == []


# ---- Wilson ---------------------------------------------------------------------------------


def test_wilson_half_and_half() -> None:
    low, high = wilson_interval(50, 100)
    assert (round(low, 4), round(high, 4)) == (0.4038, 0.5962)


def test_wilson_at_the_edges() -> None:
    assert wilson_interval(0, 10)[0] == 0.0
    assert round(wilson_interval(0, 10)[1], 4) == 0.2775
    assert round(wilson_interval(10, 10)[0], 4) == 0.7225
    assert wilson_interval(10, 10)[1] == 1.0


def test_wilson_contains_the_point_estimate() -> None:
    for k in range(0, 41, 5):
        low, high = wilson_interval(k, 40)
        assert low <= k / 40 <= high
        assert not math.isnan(low)


def test_wilson_rejects_bad_counts() -> None:
    with pytest.raises(ValueError, match="positive"):
        wilson_interval(5, 0)
    with pytest.raises(ValueError, match="between"):
        wilson_interval(11, 10)
