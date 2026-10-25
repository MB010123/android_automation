"""Open About Phone, capture a screenshot, and return IMEI/EID metadata."""
from __future__ import annotations

import base64
import logging
import re
from typing import Any, Callable

from application.device_screen_prepare import WAKE_UNLOCK_COMMANDS, run_best_effort_shell
from application.farm_task_types import FarmTaskRequest, FarmTaskResult
from domain.farm import MAX_FARM_SLOT_ID
from infrastructure.adb_companion import AdbCommandError, AdbCommandRunner
from infrastructure.adb_imei import AdbImeiReader

logger = logging.getLogger("farm_agent.about_phone")

ABOUT_PHONE_TASK = "device_about_phone_screenshot"
ABOUT_PHONE_TIMEOUT_SECONDS = 30.0
_EID_RE = re.compile(r"\b(89\d{18,32})\b")
_MINIMAL_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00"
    b"\x00\x04\x00\x01\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _serial_for_slot(slot_map: dict[int, str], farm_slot_id: int) -> str | None:
    serial = str(slot_map.get(int(farm_slot_id)) or "").strip()
    return serial or None


def _read_eid(runner: AdbCommandRunner, serial: str, warnings: list[str]) -> str | None:
    for prop in ("persist.radio.eid", "ril.eid", "ro.ril.eid"):
        try:
            value = runner.run(serial, ["shell", "getprop", prop]).stdout.strip()
        except AdbCommandError:
            continue
        match = _EID_RE.search(value)
        if match:
            return match.group(1)
    try:
        dump = runner.run(serial, ["shell", "dumpsys euicc"]).stdout
    except AdbCommandError as exc:
        warnings.append(f"eid_dumpsys: {exc.__class__.__name__}")
        return None
    match = _EID_RE.search(dump or "")
    return match.group(1) if match else None


def _screencap_png(runner: AdbCommandRunner, serial: str, warnings: list[str]) -> bytes:
    try:
        raw = runner.run_bytes(serial, ["exec-out", "screencap", "-p"])
    except (AdbCommandError, AttributeError) as exc:
        warnings.append(f"screencap: {exc.__class__.__name__}")
        return _MINIMAL_PNG
    if raw.startswith(b"\x89PNG"):
        return raw
    warnings.append("screencap_invalid")
    return _MINIMAL_PNG


def run_device_about_phone_screenshot(
    *,
    adb_path: str,
    slot_map: dict[int, str],
    request: FarmTaskRequest,
    command_runner: AdbCommandRunner | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> FarmTaskResult:
    slot_id = int(request.farm_slot_id)
    sleeper = sleep_fn or __import__("time").sleep
    if not 1 <= slot_id <= MAX_FARM_SLOT_ID:
        return FarmTaskResult(ok=False, http_status=404, error="slot_not_found")
    serial = _serial_for_slot(slot_map, slot_id)
    if not serial:
        return FarmTaskResult(ok=False, http_status=404, error="slot_not_found")
    runner = command_runner or AdbCommandRunner(adb_path, timeout_seconds=ABOUT_PHONE_TIMEOUT_SECONDS)
    try:
        state = runner.run(serial, ["get-state"]).stdout.strip()
    except AdbCommandError:
        state = ""
    if state != "device":
        return FarmTaskResult(ok=False, http_status=409, error="device_offline")

    warnings: list[str] = []
    run_best_effort_shell(runner, serial, WAKE_UNLOCK_COMMANDS, warnings, "wake_unlock")
    run_best_effort_shell(
        runner,
        serial,
        ["am start -a android.settings.DEVICE_INFO_SETTINGS"],
        warnings,
        "about_phone",
    )
    sleeper(1.5)
    png = _screencap_png(runner, serial, warnings)
    image_b64 = "data:image/png;base64," + base64.b64encode(png).decode("ascii")

    imei1 = None
    imei2 = None
    try:
        probe = AdbImeiReader(runner).read(serial)
        imei1 = probe.imei1
        imei2 = probe.imei2
        if probe.error:
            warnings.append("imei_probe_incomplete")
    except Exception:
        warnings.append("imei_probe_failed")
    eid = _read_eid(runner, serial, warnings)
    logger.info("about_phone_ok slot=%s imei1=%s eid=%s", slot_id, bool(imei1), bool(eid))
    details: dict[str, Any] = {
        "ok": True,
        "slot_id": slot_id,
        "serial": serial,
        "imei1": imei1,
        "imei2": imei2,
        "eid": eid,
        "image_base64": image_b64,
        "warnings": warnings,
    }
    return FarmTaskResult(ok=True, http_status=200, details=details)
