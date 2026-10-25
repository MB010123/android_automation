"""Toggle Airplane Mode on unrooted Pixels and Samsung One UI via ADB.

Primary path: ``cmd connectivity airplane-mode enable|disable`` (Android 11–14
shell, no root). Fallback: ``settings put global airplane_mode_on`` plus a
best-effort Quick Settings expand/collapse. Never uses EuiccManager or root.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from application.device_screen_prepare import run_best_effort_shell
from application.farm_task_types import FarmTaskRequest, FarmTaskResult
from domain.farm import MAX_FARM_SLOT_ID
from infrastructure.adb_companion import AdbCommandError, AdbCommandRunner

logger = logging.getLogger("farm_agent.airplane_mode")

AIRPLANE_MODE_TASK = "airplane_mode"
AIRPLANE_CYCLE_TASK = "airplane_cycle"
AIRPLANE_TIMEOUT_SECONDS = 20.0


def _serial_for_slot(slot_map: dict[int, str], farm_slot_id: int) -> str | None:
    serial = str(slot_map.get(int(farm_slot_id)) or "").strip()
    return serial or None


def normalize_airplane_state(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"1", "on", "enable", "enabled", "true"}:
        return "on"
    if text in {"0", "off", "disable", "disabled", "false"}:
        return "off"
    return "toggle"


def parse_airplane_state(stdout: str) -> str | None:
    text = (stdout or "").strip().lower()
    if "enable" in text:
        return "on"
    if "disable" in text:
        return "off"
    if text in {"1", "true"}:
        return "on"
    if text in {"0", "false"}:
        return "off"
    return None


def _read_state(runner: AdbCommandRunner, serial: str) -> str | None:
    try:
        out = runner.run(serial, ["shell", "cmd connectivity airplane-mode"]).stdout
        parsed = parse_airplane_state(out)
        if parsed is not None:
            return parsed
    except AdbCommandError:
        pass
    try:
        out = runner.run(serial, ["shell", "settings get global airplane_mode_on"]).stdout
        return parse_airplane_state(out)
    except AdbCommandError:
        return None


def _cmd_connectivity_available(runner: AdbCommandRunner, serial: str) -> bool:
    try:
        out = runner.run(serial, ["shell", "cmd connectivity airplane-mode"]).stdout.lower()
    except AdbCommandError as exc:
        text = str(exc).lower()
        if "unknown command" in text or "not found" in text:
            return False
        return True
    return "unknown command" not in out and "not found" not in out


def _set_via_connectivity(runner: AdbCommandRunner, serial: str, desired: str) -> bool:
    verb = "enable" if desired == "on" else "disable"
    try:
        runner.run(serial, ["shell", f"cmd connectivity airplane-mode {verb}"])
        return True
    except AdbCommandError:
        logger.info("airplane_cmd_connectivity_failed serial_suffix=%s", serial[-4:])
        return False


def _set_via_settings_fallback(
    runner: AdbCommandRunner,
    serial: str,
    desired: str,
    warnings: list[str],
) -> None:
    flag = "1" if desired == "on" else "0"
    run_best_effort_shell(
        runner,
        serial,
        [
            f"settings put global airplane_mode_on {flag}",
            "cmd statusbar expand-settings",
            "cmd statusbar collapse",
        ],
        warnings,
        "airplane_settings_fallback",
    )


def apply_airplane_mode(
    runner: AdbCommandRunner,
    serial: str,
    desired: str,
    warnings: list[str],
) -> str | None:
    if _cmd_connectivity_available(runner, serial):
        if not _set_via_connectivity(runner, serial, desired):
            _set_via_settings_fallback(runner, serial, desired, warnings)
    else:
        warnings.append("cmd_connectivity_unavailable")
        _set_via_settings_fallback(runner, serial, desired, warnings)
    return _read_state(runner, serial)


def run_airplane_mode(
    *,
    adb_path: str,
    slot_map: dict[int, str],
    request: FarmTaskRequest,
    command_runner: AdbCommandRunner | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> FarmTaskResult:
    slot_id = int(request.farm_slot_id)
    if not 1 <= slot_id <= MAX_FARM_SLOT_ID:
        return FarmTaskResult(ok=False, http_status=404, error="slot_not_found")
    serial = _serial_for_slot(slot_map, slot_id)
    if not serial:
        return FarmTaskResult(ok=False, http_status=404, error="slot_not_found")
    runner = command_runner or AdbCommandRunner(adb_path, timeout_seconds=AIRPLANE_TIMEOUT_SECONDS)
    try:
        state = runner.run(serial, ["get-state"]).stdout.strip()
    except AdbCommandError:
        state = ""
    if state != "device":
        return FarmTaskResult(ok=False, http_status=409, error="device_offline")

    warnings: list[str] = []
    requested = normalize_airplane_state(request.payload.get("state"))
    current = _read_state(runner, serial)
    if requested == "toggle":
        desired = "off" if current == "on" else "on"
    else:
        desired = requested
    applied = apply_airplane_mode(runner, serial, desired, warnings)
    logger.info("airplane_mode_ok slot=%s desired=%s applied=%s", slot_id, desired, applied)
    return FarmTaskResult(
        ok=True,
        http_status=200,
        details={
            "ok": True,
            "slot_id": slot_id,
            "serial": serial,
            "requested": requested,
            "state": applied or desired,
            "method": "cmd_connectivity" if "cmd_connectivity_unavailable" not in warnings else "settings_fallback",
            "warnings": warnings,
        },
    )


def run_airplane_cycle(
    *,
    adb_path: str,
    slot_map: dict[int, str],
    request: FarmTaskRequest,
    command_runner: AdbCommandRunner | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> FarmTaskResult:
    sleeper = sleep_fn or __import__("time").sleep
    try:
        delay = float(request.payload.get("on_seconds") or 2.0)
    except (TypeError, ValueError):
        delay = 2.0
    delay = max(0.0, min(delay, 15.0))
    on_request = FarmTaskRequest(
        job_id=request.job_id,
        task_type=AIRPLANE_MODE_TASK,
        farm_slot_id=request.farm_slot_id,
        payload={"state": "on"},
    )
    first = run_airplane_mode(
        adb_path=adb_path,
        slot_map=slot_map,
        request=on_request,
        command_runner=command_runner,
    )
    if not first.ok:
        return first
    if delay:
        sleeper(delay)
    off_request = FarmTaskRequest(
        job_id=request.job_id,
        task_type=AIRPLANE_MODE_TASK,
        farm_slot_id=request.farm_slot_id,
        payload={"state": "off"},
    )
    second = run_airplane_mode(
        adb_path=adb_path,
        slot_map=slot_map,
        request=off_request,
        command_runner=command_runner,
    )
    details = dict(second.details or {})
    details["cycled"] = True
    details["on_state"] = (first.details or {}).get("state")
    return FarmTaskResult(
        ok=second.ok,
        http_status=second.http_status,
        error=second.error,
        details=details,
    )
