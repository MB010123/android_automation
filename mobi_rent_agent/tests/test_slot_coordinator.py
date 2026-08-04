"""Unit tests for the per-slot mutex shared by provisioning/proxy/health.

This is what guarantees provisioning, proxy reconciliation, and the health
monitor's isolated reboot can never touch the same physical slot at once.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from application.slot_coordinator import SlotOperationCoordinator


def test_non_blocking_acquire_fails_while_slot_is_held():
    coordinator = SlotOperationCoordinator([1])

    with coordinator.acquire(1):
        with coordinator.acquire(1, blocking=False) as acquired:
            assert acquired is False


def test_lock_is_released_on_context_exit_and_can_be_reacquired():
    coordinator = SlotOperationCoordinator([1])

    with coordinator.acquire(1):
        pass

    with coordinator.acquire(1, blocking=False) as acquired:
        assert acquired is True


def test_other_slots_are_independent():
    coordinator = SlotOperationCoordinator([1, 2])

    with coordinator.acquire(1):
        with coordinator.acquire(2, blocking=False) as acquired:
            assert acquired is True


def test_lock_is_released_even_if_the_block_raises():
    coordinator = SlotOperationCoordinator([1])

    try:
        with coordinator.acquire(1):
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    with coordinator.acquire(1, blocking=False) as acquired:
        assert acquired is True


def test_blocking_acquire_waits_for_the_holder_to_release():
    coordinator = SlotOperationCoordinator([1])
    released = threading.Event()
    acquired_after_wait = threading.Event()

    def holder():
        with coordinator.acquire(1):
            released.wait(timeout=5)

    def waiter():
        with coordinator.acquire(1, blocking=True) as acquired:
            if acquired:
                acquired_after_wait.set()

    holder_thread = threading.Thread(target=holder)
    holder_thread.start()
    waiter_thread = threading.Thread(target=waiter)
    waiter_thread.start()

    released.set()
    holder_thread.join(timeout=5)
    waiter_thread.join(timeout=5)

    assert acquired_after_wait.is_set()
