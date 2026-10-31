"""USB/Wi-Fi priority while customer or admin streams are live."""
from __future__ import annotations

import threading
import time
from typing import Any

from infrastructure.farm_status_snapshot import DEFAULT_REFRESH_SECONDS

SNAPSHOT_RELAXED_SECONDS = 10.0
RECENT_INPUT_SKIP_SECONDS = 1.0
INPUT_BUSY_HOLD_SECONDS = 60.0

_lock = threading.Lock()
_last_input: dict[int, float] = {}
_snapshots: list[Any] = []
_customer_busy = False
_admin_slots: set[int] = set()


def register_snapshot(snapshot: Any) -> None:
    if snapshot is None:
        return
    with _lock:
        if snapshot not in _snapshots:
            _snapshots.append(snapshot)
        _apply_intervals_locked()


def set_customer_streams_busy(busy: bool) -> None:
    with _lock:
        global _customer_busy
        _customer_busy = bool(busy)
        _apply_intervals_locked()


def set_admin_slot(slot_id: int, active: bool) -> None:
    with _lock:
        bay = int(slot_id)
        if active:
            _admin_slots.add(bay)
        else:
            _admin_slots.discard(bay)
        _apply_intervals_locked()


def note_slot_input(slot_id: int, *, now: float | None = None) -> None:
    stamp = time.monotonic() if now is None else float(now)
    with _lock:
        _last_input[int(slot_id)] = stamp
        _apply_intervals_locked()


def should_skip_ui_probe(slot_id: int, *, now: float | None = None) -> bool:
    stamp = time.monotonic() if now is None else float(now)
    with _lock:
        last = _last_input.get(int(slot_id))
    if last is None:
        return False
    return (stamp - last) < RECENT_INPUT_SKIP_SECONDS


def should_skip_any_ui_probe(*, now: float | None = None) -> bool:
    stamp = time.monotonic() if now is None else float(now)
    with _lock:
        return any((stamp - last) < RECENT_INPUT_SKIP_SECONDS for last in _last_input.values())


def streams_are_busy(*, now: float | None = None) -> bool:
    stamp = time.monotonic() if now is None else float(now)
    with _lock:
        if _customer_busy or _admin_slots:
            return True
        return any((stamp - last) < INPUT_BUSY_HOLD_SECONDS for last in _last_input.values())


def _apply_intervals_locked() -> None:
    stamp = time.monotonic()
    busy = bool(_customer_busy or _admin_slots)
    if not busy:
        busy = any((stamp - last) < INPUT_BUSY_HOLD_SECONDS for last in _last_input.values())
    interval = SNAPSHOT_RELAXED_SECONDS if busy else DEFAULT_REFRESH_SECONDS
    for snapshot in list(_snapshots):
        setter = getattr(snapshot, "set_interval", None)
        if callable(setter):
            setter(interval)
