"""VPS-side heartbeat: periodically poll Farm `GET /agent/health` and persist per-bay ADB state.

Reuses the existing farm status fetcher (no new Farm endpoint). Emits
`device_online` / `device_offline` slot events only on transitions so the
event log is not flooded. Farm unreachable is recorded as `farm_error`
without changing `adb_online` (last known ADB state is preserved and the
heartbeat becomes stale for the status API).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

import requests

from infrastructure.slot_event_store import SlotEventStore
from infrastructure.slot_status_store import SlotStatusStore

logger = logging.getLogger("vps_backend.heartbeat")

FarmStatusFetcher = Callable[[], dict[str, Any]]


class FarmHeartbeatPoller:
    def __init__(
        self,
        *,
        farm_status_fetcher: FarmStatusFetcher,
        status_store: SlotStatusStore,
        event_store: SlotEventStore,
        known_farm_slots: set[int],
        interval_seconds: float = 30.0,
    ) -> None:
        self._fetch = farm_status_fetcher
        self._status = status_store
        self._events = event_store
        self._slots = set(known_farm_slots)
        self._interval = max(1.0, interval_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def interval_seconds(self) -> float:
        return self._interval

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="vps-farm-heartbeat")
        self._thread.start()

    def stop(self, join_timeout: float | None = None) -> None:
        """Signal the loop to exit; optionally wait (bounded) for it to finish."""
        self._stop.set()
        thread = self._thread
        if join_timeout is not None and thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, join_timeout))

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # pragma: no cover - defensive
                logger.exception("heartbeat_poll_unexpected_error")
            self._stop.wait(self._interval)

    def poll_once(self, *, now: float | None = None) -> dict[str, Any]:
        ts = now if now is not None else time.time()
        try:
            farm = self._fetch()
        except (requests.RequestException, OSError, ConnectionError) as exc:
            logger.warning("heartbeat_farm_unreachable error=%s", type(exc).__name__)
            return self._record_farm_error("farm_unreachable", ts)
        if not isinstance(farm, dict) or not farm.get("ok"):
            error = str((farm or {}).get("error") or "farm_not_ok") if isinstance(farm, dict) else "farm_not_ok"
            logger.warning("heartbeat_farm_not_ok error=%s", error)
            return self._record_farm_error(error, ts)

        offline = {int(s) for s in (farm.get("offline_slots") or [])}
        transitions: list[tuple[int, str]] = []
        for bay in sorted(self._slots):
            previous = self._status.get(bay)
            online = bay not in offline
            self._status.record(bay, adb_online=online, farm_ok=True, farm_error=None, now=ts)
            if previous is None:
                continue
            if previous.adb_online and not online:
                self._events.append(bay, "device_offline", "heartbeat: adb not reachable")
                transitions.append((bay, "device_offline"))
            elif not previous.adb_online and online:
                self._events.append(bay, "device_online", "heartbeat: adb reachable")
                transitions.append((bay, "device_online"))
        if transitions:
            logger.info("heartbeat_transitions count=%s", len(transitions))
        return {"ok": True, "checked": len(self._slots), "offline": sorted(offline), "transitions": transitions}

    def _record_farm_error(self, error: str, ts: float) -> dict[str, Any]:
        for bay in sorted(self._slots):
            previous = self._status.get(bay)
            adb_online = previous.adb_online if previous is not None else False
            self._status.record(bay, adb_online=adb_online, farm_ok=False, farm_error=error, now=ts)
        return {"ok": False, "error": error, "checked": len(self._slots)}
