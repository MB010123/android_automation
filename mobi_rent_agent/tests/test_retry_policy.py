"""Unit tests for the exponential backoff curve used by HeartbeatService."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from application.retry_policy import RetryPolicy


def test_zero_or_negative_failures_means_no_delay():
    policy = RetryPolicy()

    assert policy.delay_for_attempt(0) == 0.0
    assert policy.delay_for_attempt(-1) == 0.0


def test_delay_grows_by_multiplier_each_attempt():
    policy = RetryPolicy(base_delay_seconds=2.0, max_delay_seconds=1000.0, multiplier=2.0)

    assert policy.delay_for_attempt(1) == 2.0
    assert policy.delay_for_attempt(2) == 4.0
    assert policy.delay_for_attempt(3) == 8.0
    assert policy.delay_for_attempt(4) == 16.0


def test_delay_is_capped_at_max_delay_seconds():
    policy = RetryPolicy(base_delay_seconds=2.0, max_delay_seconds=10.0, multiplier=2.0)

    assert policy.delay_for_attempt(10) == 10.0
    assert policy.delay_for_attempt(100) == 10.0
