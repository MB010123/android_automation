"""Simple in-process rate limiter for VPS SMS API."""
from __future__ import annotations

import threading
import time
from collections import deque


class VpsRateLimiter:
    def __init__(
        self,
        *,
        per_slot_limit: int = 10,
        per_slot_window_seconds: float = 60.0,
        global_limit: int = 120,
        global_window_seconds: float = 60.0,
    ) -> None:
        self._per_slot_limit = max(1, per_slot_limit)
        self._per_slot_window = per_slot_window_seconds
        self._global_limit = max(1, global_limit)
        self._global_window = global_window_seconds
        self._lock = threading.Lock()
        self._slot_hits: dict[int, deque[float]] = {}
        self._global_hits: deque[float] = deque()

    def check(self, farm_slot_id: int) -> tuple[bool, int]:
        """Return (allowed, retry_after_seconds)."""
        now = time.time()
        with self._lock:
            self._prune(self._global_hits, now, self._global_window)
            if len(self._global_hits) >= self._global_limit:
                retry = int(max(1, self._retry_after(self._global_hits, now, self._global_window)))
                return False, retry
            hits = self._slot_hits.setdefault(farm_slot_id, deque())
            self._prune(hits, now, self._per_slot_window)
            if len(hits) >= self._per_slot_limit:
                retry = int(max(1, self._retry_after(hits, now, self._per_slot_window)))
                return False, retry
            self._global_hits.append(now)
            hits.append(now)
            return True, 0

    @staticmethod
    def _prune(q: deque[float], now: float, window: float) -> None:
        cutoff = now - window
        while q and q[0] < cutoff:
            q.popleft()

    @staticmethod
    def _retry_after(q: deque[float], now: float, window: float) -> float:
        if not q:
            return window
        return (q[0] + window) - now
