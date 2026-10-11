"""Wald sequential probability ratio test for the canary (spec §5.12).

H0: failure rate = p0 (the host's baseline). H1: failure rate = p1 (a regression, p1 = 2·p0).
Boundaries A = ln((1−β)/α) and B = ln(β/(1−α)). Each failure adds ln(p1/p0); each success adds
ln((1−p1)/(1−p0)). Stop and **reject** H0 (revert the fix) when LLR ≥ A; stop and **accept** H0
(promote the fix) when LLR ≤ B.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class Decision(StrEnum):
    CONTINUE = "continue"
    ACCEPT = "accept"  # H0 holds: no regression, promote
    REJECT = "reject"  # H1: regression, revert


@dataclass(frozen=True)
class SPRTResult:
    decision: Decision
    n: int
    llr: float
    path: tuple[float, ...]


@dataclass(frozen=True)
class SPRT:
    p0: float
    p1: float
    alpha: float = 0.05
    beta: float = 0.10

    def __post_init__(self) -> None:
        if not 0.0 < self.p0 < self.p1 < 1.0:
            raise ValueError("need 0 < p0 < p1 < 1")
        if not (0.0 < self.alpha < 1.0 and 0.0 < self.beta < 1.0):
            raise ValueError("alpha and beta must be in (0, 1)")

    @classmethod
    def regression(
        cls, p0: float, *, ratio: float = 2.0, alpha: float = 0.05, beta: float = 0.10
    ) -> SPRT:
        """The spec's canary: H1 is a ``ratio``-fold failure rate (default p1 = 2·p0)."""
        return cls(p0=p0, p1=ratio * p0, alpha=alpha, beta=beta)

    @property
    def upper(self) -> float:
        """A = ln((1−β)/α)."""
        return math.log((1.0 - self.beta) / self.alpha)

    @property
    def lower(self) -> float:
        """B = ln(β/(1−α))."""
        return math.log(self.beta / (1.0 - self.alpha))

    @property
    def failure_step(self) -> float:
        return math.log(self.p1 / self.p0)

    @property
    def success_step(self) -> float:
        return math.log((1.0 - self.p1) / (1.0 - self.p0))

    def step(self, failed: bool) -> float:
        return self.failure_step if failed else self.success_step

    def decide(self, llr: float) -> Decision:
        if llr >= self.upper:
            return Decision.REJECT
        if llr <= self.lower:
            return Decision.ACCEPT
        return Decision.CONTINUE

    def run(self, failures: Iterable[bool]) -> SPRTResult:
        """Feed outcomes in order (``True`` = the run failed) until a boundary is crossed."""
        llr = 0.0
        path: list[float] = []
        decision = Decision.CONTINUE
        for failed in failures:
            llr += self.step(failed)
            path.append(llr)
            decision = self.decide(llr)
            if decision is not Decision.CONTINUE:
                break
        return SPRTResult(decision=decision, n=len(path), llr=llr, path=tuple(path))

    def runs_to_accept_without_failures(self) -> int:
        """The fewest consecutive successes that accept H0 from a fresh start."""
        return self.run(iter(lambda: False, True)).n
