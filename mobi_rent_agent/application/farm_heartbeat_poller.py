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

from application.vps_farm_inventory import farm_agent_unavailable, mapped_farm_slots, offline_farm_slots
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
        after_poll: Callable[[], None] | None = None,
    ) -> None:
        self._fetch = farm_status_fetcher
        self._status = status_store
        self._events = event_store
        self._slots = set(known_farm_slots)
        self._interval = max(1.0, interval_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._after_poll = after_poll

    def set_after_poll(self, callback: Callable[[], None] | None) -> None:
        self._after_poll = callback

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
            result = self._record_farm_error("farm_unreachable", ts)
            self._run_after_poll()
            return result
        if farm_agent_unavailable(farm):
            error = (
                str(farm.get("error") or "farm_not_ok") if isinstance(farm, dict) else "farm_not_ok"
            )
            logger.warning("heartbeat_farm_not_ok error=%s", error)
            mapped = mapped_farm_slots(farm) if isinstance(farm, dict) else frozenset()
            result = self._record_farm_error(error, ts, slots=set(mapped) if mapped else None)
            self._run_after_poll()
            return result

        mapped = mapped_farm_slots(farm)
        offline = offline_farm_slots(farm)
        transitions: list[tuple[int, str]] = []
        for bay in sorted(mapped):
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
        self._run_after_poll()
        return {"ok": True, "checked": len(mapped), "offline": sorted(offline), "transitions": transitions}

    def _run_after_poll(self) -> None:
        if self._after_poll is None:
            return
        try:
            self._after_poll()
        except Exception:  # pragma: no cover - sweep must not kill heartbeat
            logger.warning("heartbeat_after_poll_failed")

    def _record_farm_error(self, error: str, ts: float, *, slots: set[int] | None = None) -> dict[str, Any]:
        bays = slots if slots is not None else self._slots
        for bay in sorted(bays):
            previous = self._status.get(bay)
            adb_online = previous.adb_online if previous is not None else False
            self._status.record(bay, adb_online=adb_online, farm_ok=False, farm_error=error, now=ts)
        return {"ok": False, "error": error, "checked": len(bays)}
