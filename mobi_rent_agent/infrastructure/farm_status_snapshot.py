"""Background-refreshed in-memory snapshot for farm /admin list APIs.

`adb devices` across a 21-bay hub can take several seconds. Serving
`/agent/health`, `/farm/status`, and `/admin/devices` from a snapshot that
a daemon refreshes every 3–5s keeps list requests under 100ms.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable

logger = logging.getLogger("farm_status_snapshot")

DEFAULT_REFRESH_SECONDS = 4.0


class FarmStatusSnapshot:
    def __init__(
        self,
        builder: Callable[[], dict[str, Any]],
        *,
        interval_seconds: float = DEFAULT_REFRESH_SECONDS,
        log: logging.Logger | None = None,
    ) -> None:
        self._builder = builder
        self._interval = max(1.0, float(interval_seconds))
        self._log = log or logger
        self._lock = threading.Lock()
        self._body: dict[str, Any] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def get(self) -> dict[str, Any]:
        with self._lock:
            if self._body is not None:
                return dict(self._body)
        self.refresh_once()
        with self._lock:
            return dict(self._body or {"ok": False, "error": "farm_status_unavailable"})

    def refresh_once(self) -> None:
        try:
            body = self._builder()
        except Exception:
            self._log.warning("farm_status_snapshot_refresh_failed", exc_info=True)
            with self._lock:
                if self._body is None:
                    self._body = {"ok": False, "error": "farm_unreachable"}
            return
        if not isinstance(body, dict):
            body = {"ok": False, "error": "invalid_farm_response"}
        with self._lock:
            self._body = dict(body)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self.refresh_once()
        self._thread = threading.Thread(
            target=self._loop,
            name="farm-status-snapshot",
            daemon=True,
        )
        self._thread.start()

    def stop(self, join_timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=join_timeout)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            self.refresh_once()
