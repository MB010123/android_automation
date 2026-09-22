"""Per-slot operation locks shared by provisioning, routing, and recovery."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from collections.abc import Generator


class SlotOperationCoordinator:
    def __init__(self, slot_ids: list[int], max_concurrent: int | None = None) -> None:
        self._locks = {slot_id: threading.Lock() for slot_id in slot_ids}
        if max_concurrent is not None and max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        self._global = threading.BoundedSemaphore(max_concurrent) if max_concurrent else None

    @contextmanager
    def acquire(self, slot_id: int, blocking: bool = True) -> Generator[bool, None, None]:
        lock = self._locks[slot_id]
        if self._global is None:
            acquired = lock.acquire(blocking=blocking)
            try:
                yield acquired
            finally:
                if acquired:
                    lock.release()
            return

        global_ok = self._global.acquire(blocking=blocking)
        if not global_ok:
            yield False
            return
        try:
            acquired = lock.acquire(blocking=blocking)
            try:
                yield acquired
            finally:
                if acquired:
                    lock.release()
        finally:
            self._global.release()
