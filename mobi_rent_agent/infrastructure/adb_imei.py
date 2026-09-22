"""Read IMEI / IMEI2 through official ADB telephony commands.

Uses ``cmd phone get-imei -s <modem-slot>``. On Android 10+ the unprivileged
ADB ``shell`` uid is expected to receive Permission denied. This module does
not use binder ``service call`` workarounds.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

from infrastructure.adb_companion import AdbCommandRunner
from infrastructure.imei import parse_cmd_phone_imei, redact_imei

logger = logging.getLogger("mobi_rent_agent.imei")


@dataclass(frozen=True)
class ImeiProbeResult:
    imei1_accessible: bool
    imei2_accessible: bool
    imei1: str | None
    imei2: str | None
    source: str
    error: str | None
    esim_imei_slot: int = 1


class AdbImeiReader:
    def __init__(self, runner: AdbCommandRunner) -> None:
        self._runner = runner

    def read(self, serial: str) -> ImeiProbeResult:
        errors: list[str] = []
        imei1 = self._read_slot(serial, 0, errors)
        imei2 = self._read_slot(serial, 1, errors)
        logger.info(
            "IMEI probe serial=%s imei1=%s imei2=%s",
            serial,
            "yes" if imei1 else "no",
            "yes" if imei2 else "no",
        )
        if imei1:
            logger.debug("IMEI1 redacted=%s", redact_imei(imei1))
        if imei2:
            logger.debug("IMEI2 redacted=%s", redact_imei(imei2))
        return ImeiProbeResult(
            imei1_accessible=imei1 is not None,
            imei2_accessible=imei2 is not None,
            imei1=imei1,
            imei2=imei2,
            source="cmd phone get-imei",
            error="; ".join(errors) or None,
        )

    def _read_slot(self, serial: str, slot_index: int, errors: list[str]) -> str | None:
        command = [
            self._runner._adb_path,
            "-s",
            serial,
            "shell",
            "cmd",
            "phone",
            "get-imei",
            "-s",
            str(slot_index),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            errors.append(f"slot {slot_index}: adb failed")
            return None
        combined = "\n".join(
            part.strip()
            for part in ((completed.stdout or ""), (completed.stderr or ""))
            if part.strip()
        )
        try:
            return parse_cmd_phone_imei(combined)
        except PermissionError:
            errors.append(f"slot {slot_index}: Permission denied")
            return None
