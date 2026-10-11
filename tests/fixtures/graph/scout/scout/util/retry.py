class RateLimited(Exception):
    pass


def retry_with_backoff(fn, attempts=3):
    for attempt in range(attempts):
        try:
            return fn()
        except RateLimited:
            if attempt == attempts - 1:
                raise
    return None
