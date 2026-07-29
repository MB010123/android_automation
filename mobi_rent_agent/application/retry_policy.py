"""Retry/backoff policy used by the heartbeat loop.

Kept isolated so it can be unit tested without any network or ADB
dependency, and so the backoff curve can be tuned without touching the
use case logic.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff with a ceiling.

    Attributes:
        base_delay_seconds: Delay used after the first failure.
        max_delay_seconds: Upper bound so the agent never waits "forever"
            between retries after prolonged outages.
        multiplier: Growth factor applied per consecutive failure.
    """

    base_delay_seconds: float = 2.0
    max_delay_seconds: float = 60.0
    multiplier: float = 2.0

    def delay_for_attempt(self, consecutive_failures: int) -> float:
        """`consecutive_failures` is 1 for the first failure, 2 for the
        second, and so on."""
        if consecutive_failures <= 0:
            return 0.0
        delay = self.base_delay_seconds * (self.multiplier ** (consecutive_failures - 1))
        return min(delay, self.max_delay_seconds)
