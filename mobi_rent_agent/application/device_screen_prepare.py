"""Wake, unlock, and reset display size after reboot or when a live session starts.

Windows ``adb shell`` joins argv and drops quotes, so this sends one ``shell``
argument: ``cmd; true; cmd; true``. Individual Android denials never fail the
task. Never reboots, never touches eSIM.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Callable

from application.farm_task_types import FarmTaskRequest, FarmTaskResult
from domain.farm import MAX_FARM_SLOT_ID
from infrastructure.adb_companion import AdbCommandError, AdbCommandRunner

logger = logging.getLogger("farm_agent.device_screen_prepare")

DEVICE_SCREEN_PREPARE_TASK = "device_screen_prepare"
SCREEN_PREPARE_TIMEOUT_SECONDS = 20.0
BOOT_WAIT_TIMEOUT_SECONDS = 90.0
BOOT_POLL_SECONDS = 1.0

WAKE_UNLOCK_COMMANDS = (
    "wm size reset",
    "wm density reset",
    "settings put global stay_on_while_plugged_in 7",
    "locksettings set-disabled true",
    "input keyevent KEYCODE_WAKEUP",
    "wm dismiss-keyguard",
    "input keyevent 82",
    "input swipe 540 1800 540 400 300",
    "input keyevent KEYCODE_HOME",
)


def best_effort_shell_script(commands: tuple[str, ...] | list[str]) -> str:
    """Join device commands so a denied step cannot fail the adb shell."""
    return "; ".join(f"{command}; true" for command in commands if str(command).strip())


def run_best_effort_shell(
    runner: AdbCommandRunner,
    serial: str,
    commands: tuple[str, ...] | list[str],
    warnings: list[str],
    label: str,
) -> bool:
    script = best_effort_shell_script(commands)
    if not script:
        return True
    try:
        runner.run(serial, ["shell", script])
        return True
    except AdbCommandError as exc:
        text = str(exc).replace("\n", " ").strip()[:180]
        warnings.append(f"{label}: {text or exc.__class__.__name__}")
        logger.info("device_screen_prepare_step_skipped label=%s", label)
        return False


def _serial_for_slot(slot_map: dict[int, str], farm_slot_id: int) -> str | None:
    serial = str(slot_map.get(int(farm_slot_id)) or "").strip()
    return serial or None


def _wait_boot_completed(
    runner: AdbCommandRunner,
    serial: str,
    warnings: list[str],
    sleep_fn: Callable[[float], None],
    timeout_seconds: float,
) -> bool:
    try:
        runner.run(serial, ["wait-for-device"])
    except AdbCommandError as exc:
        warnings.append(f"wait-for-device: {exc.__class__.__name__}")
    deadline_ticks = max(1, int(timeout_seconds / BOOT_POLL_SECONDS))
    for _ in range(deadline_ticks):
        try:
            state = runner.run(serial, ["get-state"]).stdout.strip()
        except AdbCommandError:
            state = ""
        try:
            boot = runner.run(serial, ["shell", "getprop", "sys.boot_completed"]).stdout.strip()
        except AdbCommandError:
            boot = ""
        if state == "device" and boot == "1":
            return True
        sleep_fn(BOOT_POLL_SECONDS)
    warnings.append("boot_completed_timeout")
    return False


def run_device_screen_prepare(
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
    wait_boot = bool(request.payload.get("wait_boot", False))
    runner = command_runner or AdbCommandRunner(
        adb_path, timeout_seconds=SCREEN_PREPARE_TIMEOUT_SECONDS
    )
    warnings: list[str] = []
    if wait_boot:
        online = _wait_boot_completed(
            runner, serial, warnings, sleeper, BOOT_WAIT_TIMEOUT_SECONDS
        )
    else:
        try:
            online = runner.run(serial, ["get-state"]).stdout.strip() == "device"
        except AdbCommandError:
            online = False
    if not online:
        return FarmTaskResult(
            ok=False,
            http_status=409,
            error="device_offline",
            details={
                "ok": False,
                "slot_id": slot_id,
                "serial": serial,
                "status": "device_offline",
                "warnings": warnings,
            },
        )
    run_best_effort_shell(runner, serial, WAKE_UNLOCK_COMMANDS, warnings, "wake_unlock")
    package = str(request.payload.get("voidfix_package") or "").strip()
    if package:
        from application.device_voidfix_keepalive import apply_voidfix_keepalive, normalize_voidfix_package

        safe = normalize_voidfix_package(package)
        if safe:
            apply_voidfix_keepalive(runner, serial, safe, warnings, restart=False)
    logger.info(
        "device_screen_prepare_ok slot=%s wait_boot=%s warnings=%s",
        slot_id,
        wait_boot,
        len(warnings),
    )
    return FarmTaskResult(
        ok=True,
        http_status=200,
        details={
            "ok": True,
            "slot_id": slot_id,
            "serial": serial,
            "status": "screen_prepared",
            "wait_boot": wait_boot,
            "warnings": warnings,
        },
    )


def request_device_screen_prepare(
    farm_client: Any,
    slot_id: int,
    *,
    wait_boot: bool = False,
    voidfix_package: str | None = None,
) -> None:
    """Best-effort VPS → Farm unlock. Never raises; never fails the caller."""
    if farm_client is None:
        return
    payload: dict[str, Any] = {"wait_boot": bool(wait_boot)}
    package = str(voidfix_package or "").strip()
    if package:
        payload["voidfix_package"] = package
    try:
        farm_client.run_task(
            task_type=DEVICE_SCREEN_PREPARE_TASK,
            farm_slot_id=int(slot_id),
            payload=payload,
            job_id=f"screen-prep-{int(slot_id)}-{uuid.uuid4().hex[:8]}",
        )
    except Exception:
        logger.warning("device_screen_prepare_request_failed slot=%s", slot_id)
