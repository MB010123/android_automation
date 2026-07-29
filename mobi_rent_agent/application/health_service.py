"""Per-slot health monitoring with rate-limited isolated reboot recovery."""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from application.slot_coordinator import SlotOperationCoordinator
from domain.models import DeviceHealth
from domain.ports import Clock, DeviceHealthController

logger = logging.getLogger("mobi_rent_agent.health")


@dataclass(frozen=True)
class HealthServiceConfig:
    interval_seconds: float = 15.0
    failure_threshold: int = 3
    reboot_cooldown_seconds: float = 300.0
    max_workers: int = 20


class HealthService:
    def __init__(
        self,
        config: HealthServiceConfig,
        controller: DeviceHealthController,
        slot_map: dict[int, str],
        coordinator: SlotOperationCoordinator,
        clock: Clock,
    ) -> None:
        self._config = config
        self._controller = controller
        self._slot_map = slot_map
        self._coordinator = coordinator
        self._clock = clock
        self._failures = {slot_id: 0 for slot_id in slot_map}
        self._last_reboot = {slot_id: float("-inf") for slot_id in slot_map}
        self._stop_event = threading.Event()
        self._reboot_gate = threading.Lock()

    def run_forever(self) -> None:
        logger.info("Device health monitor starting")
        while not self._stop_event.is_set():
            self.run_once()
            self._stop_event.wait(self._config.interval_seconds)

    def stop(self) -> None:
        self._stop_event.set()

    def run_once(self) -> dict[int, DeviceHealth]:
        observations: dict[int, DeviceHealth] = {}
        with ThreadPoolExecutor(
            max_workers=min(self._config.max_workers, len(self._slot_map)),
            thread_name_prefix="health-slot",
        ) as executor:
            futures = {
                executor.submit(self._controller.read_health, slot_id, serial): slot_id
                for slot_id, serial in self._slot_map.items()
            }
            for future in as_completed(futures):
                slot_id = futures[future]
                try:
                    health = future.result()
                except Exception as exc:
                    logger.exception("Unexpected health probe failure for slot %s: %s", slot_id, exc)
                    health = DeviceHealth(slot_id, False, False, False, False, error=str(exc))
                observations[slot_id] = health
                self._handle_observation(health)
        return observations

    def _handle_observation(self, health: DeviceHealth) -> None:
        slot_id = health.slot_id
        if health.healthy:
            self._failures[slot_id] = 0
            return

        self._failures[slot_id] += 1
        logger.warning(
            "Slot %s health failure %d/%d: %s",
            slot_id,
            self._failures[slot_id],
            self._config.failure_threshold,
            health.error or "radio/network unhealthy",
        )
        if self._failures[slot_id] < self._config.failure_threshold:
            return
        if self._clock.monotonic() - self._last_reboot[slot_id] < self._config.reboot_cooldown_seconds:
            return

        with self._coordinator.acquire(slot_id, blocking=False) as acquired:
            if not acquired:
                logger.info("Deferring recovery for busy slot %s", slot_id)
                return
            if not self._reboot_gate.acquire(blocking=False):
                logger.info("Deferring slot %s recovery while another reboot is in progress", slot_id)
                return
            try:
                self._controller.reboot(self._slot_map[slot_id])
                self._last_reboot[slot_id] = self._clock.monotonic()
                self._failures[slot_id] = 0
                logger.warning("Issued isolated reboot for slot %s", slot_id)
            except Exception as exc:
                logger.exception("Isolated reboot failed for slot %s: %s", slot_id, exc)
            finally:
                self._reboot_gate.release()
