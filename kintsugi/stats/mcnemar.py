"""Paired efficacy for gate G4: the exact one-sided McNemar test (spec §5.12).

For each holdout task × seed pair: b = failed before and passed after; c = passed before and
failed after. Under H0, b ~ Binomial(b + c, 0.5); the fix is better when b is improbably large.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from scipy.stats import binomtest


@dataclass(frozen=True)
class McNemarResult:
    b: int
    c: int
    p: float

    @property
    def pairs_discordant(self) -> int:
        return self.b + self.c


def discordant_counts(
    passed_before: Sequence[bool], passed_after: Sequence[bool]
) -> tuple[int, int]:
    if len(passed_before) != len(passed_after):
        raise ValueError("before and after must be paired element by element")
    pairs = list(zip(passed_before, passed_after, strict=True))
    b = sum(1 for before, after in pairs if after and not before)
    c = sum(1 for before, after in pairs if before and not after)
    return b, c


def exact_one_sided_p(b: int, c: int) -> float:
    if b < 0 or c < 0:
        raise ValueError("counts must be non-negative")
    if b + c == 0:
        return 1.0  # no discordant pairs: no evidence either way
    return float(binomtest(b, b + c, 0.5, alternative="greater").pvalue)


def mcnemar(passed_before: Sequence[bool], passed_after: Sequence[bool]) -> McNemarResult:
    b, c = discordant_counts(passed_before, passed_after)
    return McNemarResult(b=b, c=c, p=exact_one_sided_p(b, c))
