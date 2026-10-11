import time


class RateLimited(Exception):
    pass


def retry_with_backoff(fn, attempts=3, base_delay=0.0):
    for attempt in range(attempts):
        try:
            return fn()
        except RateLimited:
            if attempt == attempts - 1:
                raise
            time.sleep(base_delay * 2**attempt)
    return None
