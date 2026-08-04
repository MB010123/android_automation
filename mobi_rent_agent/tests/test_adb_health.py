"""Unit tests for infrastructure.adb_health.AdbDeviceHealthController.

Uses a FakeRunner in place of AdbCommandRunner (same pattern as
test_adb_provisioner.py) so the real telephony.registry regex parsing and
health/reboot decision logic is covered without a real device.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infrastructure.adb_companion import AdbCommandError, AdbCommandResult
from infrastructure.adb_health import AdbDeviceHealthController


class FakeRunner:
    def __init__(self, responses: dict[tuple, AdbCommandResult | Exception]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, tuple]] = []

    def run(self, serial: str, arguments: list[str]) -> AdbCommandResult:
        key = tuple(arguments)
        self.calls.append((serial, key))
        result = self._responses[key]
        if isinstance(result, Exception):
            raise result
        return result


REGISTERED_REGISTRY = "dataRegState=0 voiceRegState=0 mSignalStrength=SignalStrength: rsrp=-90"
UNREGISTERED_REGISTRY = "dataRegState=1 voiceRegState=1 mSignalStrength=SignalStrength: rsrp=-999"


def test_fully_healthy_device():
    runner = FakeRunner({
        ("get-state",): AdbCommandResult("device", ""),
        ("shell", "getprop", "sys.boot_completed"): AdbCommandResult("1", ""),
        ("shell", "dumpsys", "telephony.registry"): AdbCommandResult(REGISTERED_REGISTRY, ""),
        ("shell", "ping", "-c", "1", "-W", "3", "1.1.1.1"): AdbCommandResult("1 packets received", ""),
    })
    controller = AdbDeviceHealthController(runner)

    health = controller.read_health(1, "SERIAL-1")

    assert health.healthy is True
    assert health.radio_registered is True
    assert health.network_reachable is True
    assert health.signal_summary is not None


def test_adb_offline_short_circuits_before_further_probes():
    runner = FakeRunner({("get-state",): AdbCommandResult("offline", "")})
    controller = AdbDeviceHealthController(runner)

    health = controller.read_health(1, "SERIAL-1")

    assert health.healthy is False
    assert health.adb_online is False
    assert health.error == "ADB offline"
    assert runner.calls == [("SERIAL-1", ("get-state",))]


def test_boot_not_completed_short_circuits_before_telephony_probe():
    runner = FakeRunner({
        ("get-state",): AdbCommandResult("device", ""),
        ("shell", "getprop", "sys.boot_completed"): AdbCommandResult("0", ""),
    })
    controller = AdbDeviceHealthController(runner)

    health = controller.read_health(1, "SERIAL-1")

    assert health.adb_online is True
    assert health.boot_completed is False
    assert health.healthy is False
    assert health.error == "boot incomplete"


def test_unregistered_radio_and_unreachable_network_marks_unhealthy():
    runner = FakeRunner({
        ("get-state",): AdbCommandResult("device", ""),
        ("shell", "getprop", "sys.boot_completed"): AdbCommandResult("1", ""),
        ("shell", "dumpsys", "telephony.registry"): AdbCommandResult(UNREGISTERED_REGISTRY, ""),
        ("shell", "ping", "-c", "1", "-W", "3", "1.1.1.1"): AdbCommandError("ping failed"),
    })
    controller = AdbDeviceHealthController(runner)

    health = controller.read_health(1, "SERIAL-1")

    assert health.radio_registered is False
    assert health.network_reachable is False
    assert health.healthy is False


def test_unexpected_adb_command_error_is_captured_not_raised():
    runner = FakeRunner({("get-state",): AdbCommandError("device offline unexpectedly")})
    controller = AdbDeviceHealthController(runner)

    health = controller.read_health(1, "SERIAL-1")

    assert health.healthy is False
    assert "device offline unexpectedly" in health.error


def test_reboot_delegates_to_runner():
    runner = FakeRunner({("reboot",): AdbCommandResult("", "")})
    controller = AdbDeviceHealthController(runner)

    controller.reboot("SERIAL-1")

    assert runner.calls == [("SERIAL-1", ("reboot",))]


@pytest.mark.parametrize("registry,expected", [
    ("voiceRegState=0 dataRegState=0", True),
    ("mServiceState=IN_SERVICE", True),
    ("voiceRegState=1 dataRegState=1", False),
    ("", False),
])
def test_is_registered_regex(registry, expected):
    assert AdbDeviceHealthController._is_registered(registry) is expected


def test_signal_summary_returns_last_match_truncated():
    registry = "mSignalStrength=first\nmSignalStrength=" + ("x" * 600)
    summary = AdbDeviceHealthController._signal_summary(registry)

    assert summary is not None
    assert summary.startswith("x")
    assert len(summary) == 500


def test_signal_summary_none_when_absent():
    assert AdbDeviceHealthController._signal_summary("no signal info here") is None
