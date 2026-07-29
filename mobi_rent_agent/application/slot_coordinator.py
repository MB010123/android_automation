"""Per-slot operation locks shared by provisioning, routing, and recovery."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from collections.abc import Generator


class SlotOperationCoordinator:
    def __init__(self, slot_ids: list[int]) -> None:
        self._locks = {slot_id: threading.Lock() for slot_id in slot_ids}

    @contextmanager
    def acquire(self, slot_id: int, blocking: bool = True) -> Generator[bool, None, None]:
        lock = self._locks[slot_id]
        acquired = lock.acquire(blocking=blocking)
        try:
            yield acquired
        finally:
            if acquired:
                lock.release()
