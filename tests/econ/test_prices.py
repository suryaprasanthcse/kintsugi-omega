from __future__ import annotations

import pytest

from kintsugi.econ.prices import PRICES, UnpricedModel, usage_cost, worst_case_cost


def test_list_prices_per_million_tokens() -> None:
    assert usage_cost("claude-haiku-5-5", {"input_tokens": 100_000}) == pytest.approx(0.01)
    assert usage_cost("claude-haiku-5-5", {"output_tokens": 1_000_000}) == pytest.approx(0.50)
    assert usage_cost("claude-sonnet-5-5", {"input_tokens": 10**6, "output_tokens": 10**6}) == (
        pytest.approx(12.00)
    )
    assert usage_cost("claude-opus-5-5", {"output_tokens": 1_000_000}) == pytest.approx(20.00)


def test_cache_writes_and_reads() -> None:
    split = {"cache_creation": {"ephemeral_5m_input_tokens": 10**6}}
    assert usage_cost("claude-sonnet-5-5", split) == pytest.approx(2.00 * 1.25)
    one_hour = {"cache_creation": {"ephemeral_1h_input_tokens": 10**6}}
    assert usage_cost("claude-sonnet-5-5", one_hour) == pytest.approx(4.00)
    flat = {"cache_creation_input_tokens": 10**6}  # no TTL split: the 5-minute default
    assert usage_cost("claude-sonnet-5-5", flat) == pytest.approx(2.50)
    assert usage_cost("claude-opus-5-5", {"cache_read_input_tokens": 10**6}) == pytest.approx(0.20)


def test_haiku_long_context_tier_above_100k_prompt_tokens() -> None:
    short = usage_cost("claude-haiku-5-5", {"input_tokens": 100_000, "output_tokens": 1000})
    long = usage_cost("claude-haiku-5-5", {"input_tokens": 100_001, "output_tokens": 1000})
    assert short == pytest.approx((100_000 * 0.10 + 1000 * 0.50) / 1e6)
    assert long == pytest.approx((100_001 * 0.50 + 1000 * 2.50) / 1e6)


def test_unknown_models_are_refused() -> None:
    with pytest.raises(UnpricedModel):
        usage_cost("gpt-5", {"input_tokens": 1})


@pytest.mark.parametrize("model", sorted(PRICES))
def test_the_worst_case_bounds_any_real_usage(model: str) -> None:
    body_bytes, max_tokens = 4000, 2048
    bound = worst_case_cost(model, body_bytes, max_tokens)
    worst_real = {"cache_creation": {"ephemeral_1h_input_tokens": body_bytes},
                  "output_tokens": max_tokens}  # fmt: skip
    assert usage_cost(model, worst_real) <= bound + 1e-12
