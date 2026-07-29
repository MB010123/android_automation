"""Continuously reconcile one persistent SOCKS5 VPN route per slot."""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from application.slot_coordinator import SlotOperationCoordinator
from domain.models import ProxyRoute
from domain.ports import ProxyConfigurator

logger = logging.getLogger("mobi_rent_agent.proxy")


@dataclass(frozen=True)
class ProxyServiceConfig:
    reconcile_interval_seconds: float = 30.0
    max_workers: int = 20


class ProxyService:
    def __init__(
        self,
        config: ProxyServiceConfig,
        configurator: ProxyConfigurator,
        slot_map: dict[int, str],
        routes: dict[int, ProxyRoute],
        coordinator: SlotOperationCoordinator,
    ) -> None:
        self._config = config
        self._configurator = configurator
        self._slot_map = slot_map
        self._routes = routes
        self._coordinator = coordinator
        self._stop_event = threading.Event()

    def run_forever(self) -> None:
        logger.info("SOCKS5 route reconciler starting")
        while not self._stop_event.is_set():
            self.run_once()
            self._stop_event.wait(self._config.reconcile_interval_seconds)

    def stop(self) -> None:
        self._stop_event.set()

    def run_once(self) -> dict[int, bool]:
        worker_count = min(self._config.max_workers, len(self._routes))
        outcomes: dict[int, bool] = {}
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="proxy-slot") as executor:
            futures = {
                executor.submit(self._ensure_slot, slot_id): slot_id
                for slot_id in self._routes
            }
            for future in as_completed(futures):
                slot_id = futures[future]
                try:
                    outcomes[slot_id] = future.result()
                except Exception as exc:
                    logger.exception("Unexpected proxy worker failure for slot %s: %s", slot_id, exc)
                    outcomes[slot_id] = False
        return outcomes

    def _ensure_slot(self, slot_id: int) -> bool:
        with self._coordinator.acquire(slot_id, blocking=False) as acquired:
            if not acquired:
                logger.debug("Skipping proxy reconciliation for busy slot %s", slot_id)
                return False
            try:
                self._configurator.ensure_route(self._slot_map[slot_id], self._routes[slot_id])
                return True
            except Exception as exc:
                logger.warning("SOCKS5 route unhealthy for slot %s: %s", slot_id, exc)
                return False
