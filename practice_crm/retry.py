"""Retry with exponential backoff for rate-limited external calls."""

from __future__ import annotations

import time
from collections.abc import Callable


class RateLimited(Exception):
    """An external service answered HTTP 429."""

    reason_code = "RATE_LIMITED"


def retry_with_backoff[T](
    fn: Callable[[], T], attempts: int = 5, base_delay: float = 0.02, max_delay: float = 0.5
) -> T:
    """Call ``fn`` until it returns, re-running it whole after each ``RateLimited``."""
    for attempt in range(attempts):
        try:
            return fn()
        except RateLimited:
            if attempt == attempts - 1:
                raise
            time.sleep(min(max_delay, base_delay * 2**attempt))
    raise AssertionError("unreachable")
