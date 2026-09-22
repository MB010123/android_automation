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


def test_dry_run_never_reboots_even_past_threshold():
    controller = FakeController({1: unhealthy(1)})
    service = HealthService(
        HealthServiceConfig(failure_threshold=1, recovery_enabled=False),
        controller,
        {1: "SERIAL-1"},
        SlotOperationCoordinator([1]),
        FakeClock(),
    )

    for _ in range(5):
        service.run_once()

    assert controller.reboots == []


def test_dry_run_applies_cooldown_pacing_like_real_recovery(caplog):
    clock = FakeClock()
    controller = FakeController({1: unhealthy(1)})
    service = HealthService(
        HealthServiceConfig(failure_threshold=1, reboot_cooldown_seconds=300, recovery_enabled=False),
        controller,
        {1: "SERIAL-1"},
        SlotOperationCoordinator([1]),
        clock,
    )

    with caplog.at_level("WARNING"):
        service.run_once()  # threshold hit -> dry-run warning + cooldown
        service.run_once()  # inside cooldown -> no second warning
        clock.now = 301
        service.run_once()  # cooldown expired -> second dry-run warning

    dry_run_lines = [record for record in caplog.records if "DRY-RUN" in record.getMessage()]
    assert len(dry_run_lines) == 2
    assert controller.reboots == []


def test_recovery_disabled_by_default_does_not_reboot():
    controller = FakeController({1: unhealthy(1)})
    service = HealthService(
        HealthServiceConfig(failure_threshold=1),
        controller,
        {1: "SERIAL-1"},
        SlotOperationCoordinator([1]),
        FakeClock(),
    )

    service.run_once()

    assert controller.reboots == []


def test_recovery_enabled_explicitly_still_reboots():
    controller = FakeController({1: unhealthy(1)})
    service = HealthService(
        HealthServiceConfig(failure_threshold=1, recovery_enabled=True),
        controller,
        {1: "SERIAL-1"},
        SlotOperationCoordinator([1]),
        FakeClock(),
    )

    service.run_once()

    assert controller.reboots == ["SERIAL-1"]
