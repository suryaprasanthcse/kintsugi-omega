"""Model prices and the cost of one Messages API call (spec §4.6.5 actuals, I4).

Anthropic first-party rates in USD per million tokens, cached 2026-10-06 from the Claude API
reference. Cache writes cost 1.25× the input rate (5-minute TTL) or 2× (1-hour TTL); cache
reads have their own per-model rate. Claude Haiku 5.5 doubles to a long-context tier above
100K prompt tokens. A model that is not listed here is never forwarded: the proxy must be able
to meter every call it lets through.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

CACHE_WRITE_5M = 1.25
CACHE_WRITE_1H = 2.0


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_read: float
    long_input: float | None = None
    long_output: float | None = None
    long_threshold: int | None = None  # prompt tokens above which the long tier applies

    def rates(self, prompt_tokens: int) -> tuple[float, float, float]:
        """(input, output, cache-read) rates for a prompt of this size."""
        long_input, long_output = self.long_input, self.long_output
        if (
            self.long_threshold is not None
            and prompt_tokens > self.long_threshold
            and long_input is not None
            and long_output is not None
        ):
            return long_input, long_output, self.cache_read * long_input / self.input
        return self.input, self.output, self.cache_read


PRICES: Mapping[str, Price] = {
    "claude-fable-5-1": Price(10.00, 50.00, 0.25),
    "claude-opus-5-5": Price(4.00, 20.00, 0.20),
    "claude-sonnet-5-5": Price(2.00, 10.00, 0.20),
    "claude-haiku-5-5": Price(0.10, 0.50, 0.01, 0.50, 2.50, 100_000),
    "claude-haiku-4-5": Price(1.00, 5.00, 0.10),
}


class UnpricedModel(KeyError):
    pass


def price_of(model: str) -> Price:
    try:
        return PRICES[model]
    except KeyError:
        raise UnpricedModel(model) from None


def _int(usage: Mapping[str, Any], key: str) -> int:
    value = usage.get(key)
    return int(value) if isinstance(value, int | float) else 0


def usage_cost(model: str, usage: Mapping[str, Any]) -> float:
    """USD cost of one call from its ``usage``: uncached input, cache writes/reads, output."""
    price = price_of(model)
    uncached = _int(usage, "input_tokens")
    output = _int(usage, "output_tokens")
    cache_read = _int(usage, "cache_read_input_tokens")
    split = usage.get("cache_creation")
    if isinstance(split, Mapping):
        write_5m = _int(split, "ephemeral_5m_input_tokens")
        write_1h = _int(split, "ephemeral_1h_input_tokens")
    else:  # no TTL breakdown: the default TTL is 5 minutes
        write_5m, write_1h = _int(usage, "cache_creation_input_tokens"), 0
    prompt = uncached + cache_read + write_5m + write_1h
    rate_in, rate_out, rate_read = price.rates(prompt)
    micro_usd = (
        uncached * rate_in
        + write_5m * rate_in * CACHE_WRITE_5M
        + write_1h * rate_in * CACHE_WRITE_1H
        + cache_read * rate_read
        + output * rate_out
    )
    return micro_usd / 1_000_000


def worst_case_cost(model: str, prompt_bytes: int, max_tokens: int) -> float:
    """An upper bound reserved before forwarding: every prompt byte a token written to the
    1-hour cache at the long-context rate, plus ``max_tokens`` of output."""
    price = price_of(model)
    rate_in, rate_out, _ = price.rates(10**12 if price.long_threshold else 0)
    return (prompt_bytes * rate_in * CACHE_WRITE_1H + max_tokens * rate_out) / 1_000_000
