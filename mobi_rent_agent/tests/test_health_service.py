from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from application.health_service import HealthService, HealthServiceConfig
from application.slot_coordinator import SlotOperationCoordinator
from domain.models import DeviceHealth
from domain.ports import Clock, DeviceHealthController


class FakeClock(Clock):
    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


class FakeController(DeviceHealthController):
    def __init__(self, health: dict[int, DeviceHealth]) -> None:
        self.health = health
        self.reboots: list[str] = []

    def read_health(self, slot_id: int, serial: str) -> DeviceHealth:
        return self.health[slot_id]

    def reboot(self, serial: str) -> None:
        self.reboots.append(serial)


def unhealthy(slot_id: int) -> DeviceHealth:
    return DeviceHealth(slot_id, True, True, False, False, error="not registered")


def healthy(slot_id: int) -> DeviceHealth:
    return DeviceHealth(slot_id, True, True, True, True)


def test_only_unhealthy_slot_reboots_after_threshold():
    controller = FakeController({1: unhealthy(1), 2: healthy(2)})
    service = HealthService(
        HealthServiceConfig(failure_threshold=2),
        controller,
        {1: "SERIAL-1", 2: "SERIAL-2"},
        SlotOperationCoordinator([1, 2]),
        FakeClock(),
    )

    service.run_once()
    service.run_once()

    assert controller.reboots == ["SERIAL-1"]


def test_reboot_cooldown_prevents_reboot_storm():
    clock = FakeClock()
    controller = FakeController({1: unhealthy(1)})
    service = HealthService(
        HealthServiceConfig(failure_threshold=1, reboot_cooldown_seconds=300),
        controller,
        {1: "SERIAL-1"},
        SlotOperationCoordinator([1]),
        clock,
    )

    service.run_once()
    service.run_once()
    clock.now = 301
    service.run_once()

    assert controller.reboots == ["SERIAL-1", "SERIAL-1"]


def test_recovery_is_deferred_while_slot_is_busy():
    coordinator = SlotOperationCoordinator([1])
    controller = FakeController({1: unhealthy(1)})
    service = HealthService(
        HealthServiceConfig(failure_threshold=1),
        controller,
        {1: "SERIAL-1"},
        coordinator,
        FakeClock(),
    )

    with coordinator.acquire(1):
        service.run_once()

    assert controller.reboots == []
