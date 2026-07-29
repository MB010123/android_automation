"""System clock implementation of the Clock port."""
from __future__ import annotations

import time

from domain.ports import Clock


class SystemClock(Clock):
    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)

    def monotonic(self) -> float:
        return time.monotonic()
