"""Small retry helpers shared by network-facing pipeline stages."""
from __future__ import annotations

from datetime import datetime, UTC
from email.utils import parsedate_to_datetime
from typing import Any


MAX_RETRY_AFTER_SECONDS = 300.0


def retry_reason(failure_kind: str) -> str:
    return {
        "timeout": "transport_timeout", "empty_output": "empty_response",
        "schema": "schema_invalid", "rate_limit": "rate_limit",
        "semantic_reject": "semantic_reject",
    }.get(failure_kind, "runner_error")


class RetryBudget:
    """Bound attempts; semantic rejections are observations, not transport retries."""

    def __init__(self, max_attempts: int, *, scope: str) -> None:
        self.max_attempts = int(max_attempts)
        if self.max_attempts < 0:
            raise ValueError("max_attempts must be nonnegative")
        self.scope = scope
        self.events: list[dict[str, Any]] = []
        self._used = 0

    @property
    def remaining(self) -> int:
        return self.max_attempts - self._used

    @property
    def exhausted(self) -> bool:
        return self.remaining == 0

    def consume(self, reason: str) -> bool:
        semantic = reason == "semantic_reject"
        allowed = not semantic and not self.exhausted
        self.events.append({"reason": reason, "at": datetime.now(UTC).isoformat(),
                            "consumed": allowed})
        if allowed:
            self._used += 1
        return allowed

    def metrics(self) -> dict[str, Any]:
        return {"retry_events": list(self.events), "budget_remaining": self.remaining,
                "retry_reason": self.events[-1]["reason"] if self.events else None}


def exponential_delay(attempt: int, backoff: float) -> float:
    """Return the delay before retrying after a 1-based failed attempt."""
    factor = float(2 ** max(0, attempt - 1))
    return max(0.0, float(backoff)) * factor


def retry_after_seconds(
        value: object, now: datetime | None = None,
        max_seconds: float = MAX_RETRY_AFTER_SECONDS,
) -> float | None:
    """Parse Retry-After and cap untrusted server-controlled delays."""
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    try:
        return min(max_seconds, max(0.0, float(value)))
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    return min(max_seconds, max(0.0, (target - current).total_seconds()))
