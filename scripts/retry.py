"""Small retry helpers shared by network-facing pipeline stages."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


def exponential_delay(attempt, backoff):
    """Return the delay before retrying after a 1-based failed attempt."""
    return max(0.0, float(backoff)) * (2 ** max(0, attempt - 1))


def retry_after_seconds(value, now=None):
    """Parse HTTP Retry-After seconds or date; return None when invalid."""
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return max(0.0, (target - current).total_seconds())
