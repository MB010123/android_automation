"""ADB-backed implementation of the SlotStatusProvider port.

Phase 1 scope: for each configured slot, resolve its fixed ADB serial
(from `slot_map.json`) and determine whether the device is reachable and
responsive. This keeps slot identity tied to a *physical* serial number
rather than USB enumeration order, per the architecture described for
the on-prem daemon.

Busy/locked-job detection (Phase 2: "two workers can't touch the same
slot") is intentionally left out here; `_read_lock_status` is a seam for
that to be added later without touching the rest of the class.
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Iterable

from domain.models import SlotState, SlotStatus
from domain.ports import SlotStatusProvider

logger = logging.getLogger("mobi_rent_agent.adb_slot_status")


class SlotMapError(RuntimeError):
    """Raised when the slot-to-serial mapping file is missing or invalid."""


def load_slot_map(path: str | Path) -> dict[int, str]:
    """Load the fixed slot_id -> ADB serial mapping.

    Expected file format (slot_map.json):
        {
          "1": "R58N123ABCD",
          "2": "R58N456EFGH"
        }
    """
    path = Path(path)
    if not path.exists():
        raise SlotMapError(f"Slot map file not found: {path}")

    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SlotMapError(f"Invalid JSON in slot map file {path}: {exc}") from exc

    try:
        return {int(slot_id): serial for slot_id, serial in raw.items()}
    except (TypeError, ValueError) as exc:
        raise SlotMapError(f"Slot map file {path} must be a {{slot_id: serial}} object") from exc


class AdbSlotStatusProvider(SlotStatusProvider):
    """Determines slot status by shelling out to `adb`.

    A slot is:
      - ONLINE          if its serial appears in `adb devices` as "device"
                        (authorized, responsive) and a lightweight shell
                        command succeeds.
      - NETWORK_ERROR   if the serial is missing, "offline", or
                        "unauthorized", or the shell probe fails/times out.
    """

    def __init__(self, slot_map: dict[int, str], adb_path: str = "adb", command_timeout_seconds: float = 5.0) -> None:
        self._slot_map = slot_map
        self._adb_path = adb_path
        self._timeout = command_timeout_seconds

    def read_slot_states(self) -> Iterable[SlotState]:
        adb_device_states = self._list_adb_devices()
        for slot_id, serial in self._slot_map.items():
            yield SlotState(slot_id=slot_id, status=self._resolve_status(serial, adb_device_states))

    def _resolve_status(self, serial: str, adb_device_states: dict[str, str]) -> SlotStatus:
        state = adb_device_states.get(serial)
        if state != "device":
            logger.debug("Serial %s adb state=%s -> networkerror", serial, state)
            return SlotStatus.NETWORK_ERROR

        if not self._probe_responsive(serial):
            return SlotStatus.NETWORK_ERROR

        return SlotStatus.ONLINE

    def _list_adb_devices(self) -> dict[str, str]:
        """Returns {serial: state} from `adb devices`, e.g. {"R58N...": "device"}."""
        try:
            completed = subprocess.run(
                [self._adb_path, "devices"],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            logger.error("Failed to run `adb devices`: %s", exc)
            return {}

        devices: dict[str, str] = {}
        for line in completed.stdout.splitlines()[1:]:  # skip "List of devices attached"
            line = line.strip()
            if not line or "\t" not in line:
                continue
            serial, state = line.split("\t", 1)
            devices[serial.strip()] = state.strip()
        return devices

    def _probe_responsive(self, serial: str) -> bool:
        """Lightweight liveness check: the Android build should answer a
        basic shell command within the timeout."""
        try:
            completed = subprocess.run(
                [self._adb_path, "-s", serial, "shell", "echo", "ok"],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            logger.warning("ADB probe failed for %s: %s", serial, exc)
            return False

        return completed.returncode == 0 and "ok" in completed.stdout
