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
import time
from collections.abc import Callable, Iterable
from pathlib import Path

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

    Every `read_slot_states()` call re-runs `adb devices`. The provider does
    not cache the first listing. Missing/offline/unauthorized serials are
    not probed. After a failed shell probe, further probes for that serial
    are delayed (`probe_retry_seconds`) until the ADB list state changes,
    so a ghost transport cannot stall every cycle. Recovery is resume-only:
    this class never runs `adb reconnect`, kill-server, or reboot.
    """

    def __init__(
        self,
        slot_map: dict[int, str],
        adb_path: str = "adb",
        command_timeout_seconds: float = 5.0,
        probe_retry_seconds: float = 15.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._slot_map = slot_map
        self._adb_path = adb_path
        self._timeout = command_timeout_seconds
        self._probe_retry_seconds = probe_retry_seconds
        self._monotonic = monotonic
        self._last_list_state: dict[str, str] = {}
        self._probe_blocked_until: dict[str, float] = {}

    def read_slot_states(self) -> Iterable[SlotState]:
        adb_device_states = self._list_adb_devices()
        for slot_id, serial in self._slot_map.items():
            yield SlotState(slot_id=slot_id, status=self._resolve_status(serial, adb_device_states))

    def _resolve_status(self, serial: str, adb_device_states: dict[str, str]) -> SlotStatus:
        state = adb_device_states.get(serial) or "missing"
        previous = self._last_list_state.get(serial)
        if previous is not None and previous != state:
            self._probe_blocked_until.pop(serial, None)
        self._last_list_state[serial] = state

        if state != "device":
            logger.debug("Serial %s adb state=%s -> networkerror", serial, state)
            return SlotStatus.NETWORK_ERROR

        if self._monotonic() < self._probe_blocked_until.get(serial, 0.0):
            logger.debug("Serial %s shell probe deferred after recent failure", serial)
            return SlotStatus.NETWORK_ERROR

        if not self._probe_responsive(serial):
            self._probe_blocked_until[serial] = self._monotonic() + self._probe_retry_seconds
            return SlotStatus.NETWORK_ERROR

        self._probe_blocked_until.pop(serial, None)
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
            parsed = _parse_adb_devices_line(line)
            if parsed is None:
                continue
            serial, state = parsed
            devices[serial] = state
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


def _parse_adb_devices_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line:
        return None
    if "\t" in line:
        serial, state = line.split("\t", 1)
        state = state.split()[0] if state.strip() else ""
    else:
        parts = line.split()
        if len(parts) < 2:
            return None
        serial, state = parts[0], parts[1]
    serial = serial.strip()
    state = state.strip()
    if not serial or not state:
        return None
    return serial, state
