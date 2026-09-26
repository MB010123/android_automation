"""Bounded per-key rate limiter for auth endpoints (IP / email)."""
from __future__ import annotations

import threading
import time
from collections import deque


class AuthRateLimiter:
    def __init__(
        self,
        *,
        limit: int = 10,
        window_seconds: float = 60.0,
        max_keys: int = 4096,
    ) -> None:
        self._limit = max(1, limit)
        self._window = window_seconds
        self._max_keys = max(16, max_keys)
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}

    def check(self, key: str) -> tuple[bool, int]:
        now = time.time()
        with self._lock:
            self._evict(now)
            hits = self._hits.setdefault(key, deque())
            cutoff = now - self._window
            while hits and hits[0] < cutoff:
                hits.popleft()
            if len(hits) >= self._limit:
                retry = int(max(1, (hits[0] + self._window) - now))
                return False, retry
            hits.append(now)
            return True, 0

    def _evict(self, now: float) -> None:
        if len(self._hits) < self._max_keys:
            return
        stale = [k for k, q in self._hits.items() if not q or q[-1] < now - self._window]
        for k in stale:
            del self._hits[k]
        if len(self._hits) >= self._max_keys:
            oldest = sorted(self._hits.items(), key=lambda item: item[1][0] if item[1] else 0.0)
            for k, _ in oldest[: max(1, len(oldest) // 4)]:
                del self._hits[k]
