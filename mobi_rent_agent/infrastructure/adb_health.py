"""ADB radio/network health observation and isolated reboot control."""
from __future__ import annotations

import re

from domain.models import DeviceHealth
from domain.ports import DeviceHealthController
from infrastructure.adb_companion import AdbCommandError, AdbCommandRunner


class AdbDeviceHealthController(DeviceHealthController):
    def __init__(self, runner: AdbCommandRunner) -> None:
        self._runner = runner

    def read_health(self, slot_id: int, serial: str) -> DeviceHealth:
        try:
            adb_online = self._runner.run(serial, ["get-state"]).stdout == "device"
            if not adb_online:
                return DeviceHealth(slot_id, False, False, False, False, error="ADB offline")

            boot_completed = self._runner.run(
                serial,
                ["shell", "getprop", "sys.boot_completed"],
            ).stdout == "1"
            if not boot_completed:
                return DeviceHealth(slot_id, True, False, False, False, error="boot incomplete")

            registry = self._runner.run(
                serial,
                ["shell", "dumpsys", "telephony.registry"],
            ).stdout
            radio_registered = self._is_registered(registry)
            signal_summary = self._signal_summary(registry)
            try:
                ping = self._runner.run(
                    serial,
                    ["shell", "ping", "-c", "1", "-W", "3", "1.1.1.1"],
                )
                network_reachable = "1 received" in ping.stdout or "1 packets received" in ping.stdout
            except AdbCommandError:
                network_reachable = False
            return DeviceHealth(
                slot_id=slot_id,
                adb_online=True,
                boot_completed=True,
                radio_registered=radio_registered,
                network_reachable=network_reachable,
                signal_summary=signal_summary,
            )
        except AdbCommandError as exc:
            return DeviceHealth(slot_id, False, False, False, False, error=str(exc))

    def reboot(self, serial: str) -> None:
        self._runner.run(serial, ["reboot"])

    @staticmethod
    def _is_registered(registry: str) -> bool:
        return bool(
            re.search(r"(?:voiceRegState|dataRegState)=0\b", registry)
            or re.search(r"\bIN_SERVICE\b", registry)
        )

    @staticmethod
    def _signal_summary(registry: str) -> str | None:
        matches = re.findall(r"mSignalStrength=(.+)", registry)
        return matches[-1].strip()[:500] if matches else None
