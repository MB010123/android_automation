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
from application.esim_setup_ui_state import OEM_PIXEL, OEM_SAMSUNG
from application.remote_access_farm_task import disable_native_camera_commands
from application.samsung_one_ui import disable_samsung_game_commands, is_samsung_device
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

# Samsung Galaxy S20 ships FHD+ (1080x2400 @ 420). ``wm size reset`` restores
# the physical WQHD+ panel (1440x3200), which desyncs GADS stream vs taps.
SAMSUNG_S20_SERIAL = "RFCNB056WEV"
SAMSUNG_S20_SLOT_ID = 21
SAMSUNG_S20_SIZE = "1080x2400"
SAMSUNG_S20_DENSITY = "420"
_SAMSUNG_S20_MODEL_PREFIXES = ("SM-G980", "SM-G981", "SM-G985", "SM-G986", "SM-G988")


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


def _prop(runner: AdbCommandRunner, serial: str, key: str) -> str:
    try:
        return (runner.run(serial, ["shell", "getprop", key]).stdout or "").strip()
    except AdbCommandError:
        return ""


def is_samsung_s20(
    *,
    slot_id: int,
    serial: str,
    manufacturer: str = "",
    model: str = "",
) -> bool:
    if int(slot_id) == SAMSUNG_S20_SLOT_ID:
        return True
    if str(serial or "").strip().upper() == SAMSUNG_S20_SERIAL:
        return True
    brand = str(manufacturer or "").strip().lower()
    if brand != "samsung" and not brand.startswith("samsung"):
        return False
    upper = str(model or "").strip().upper()
    return any(upper.startswith(prefix) for prefix in _SAMSUNG_S20_MODEL_PREFIXES)


def wake_unlock_commands(*, samsung_s20: bool, skip_home: bool = False) -> tuple[str, ...]:
    """Wake/unlock script. S20 stays on FHD+ so stream and taps share one size.

    ``skip_home`` is used while the customer is on the eSIM handoff screen.
    Do not reset ``wm size`` / density or swipe — those drop the GADS
    MediaProjection stream.
    """
    if skip_home:
        return (
            "settings put global stay_on_while_plugged_in 7",
            "input keyevent KEYCODE_WAKEUP",
            "wm dismiss-keyguard",
        )
    size = f"wm size {SAMSUNG_S20_SIZE}" if samsung_s20 else "wm size reset"
    density = f"wm density {SAMSUNG_S20_DENSITY}" if samsung_s20 else "wm density reset"
    return (size, density) + WAKE_UNLOCK_COMMANDS[2:]


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
    skip_home = bool(request.payload.get("skip_home", False))
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
    manufacturer = _prop(runner, serial, "ro.product.manufacturer")
    model = _prop(runner, serial, "ro.product.model")
    samsung_s20 = is_samsung_s20(
        slot_id=slot_id, serial=serial, manufacturer=manufacturer, model=model
    )
    run_best_effort_shell(
        runner,
        serial,
        wake_unlock_commands(samsung_s20=samsung_s20, skip_home=skip_home),
        warnings,
        "wake_unlock",
    )
    samsung = is_samsung_device(
        slot_id=slot_id, serial=serial, manufacturer=manufacturer
    ) or samsung_s20
    oem = OEM_SAMSUNG if samsung else OEM_PIXEL
    if samsung:
        run_best_effort_shell(
            runner,
            serial,
            disable_samsung_game_commands(),
            warnings,
            "disable_game_bloat",
        )
    run_best_effort_shell(
        runner,
        serial,
        disable_native_camera_commands(oem),
        warnings,
        "disable_camera",
    )
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
    skip_home: bool = False,
) -> None:
    """Best-effort VPS → Farm unlock. Never raises; never fails the caller."""
    if farm_client is None:
        return
    payload: dict[str, Any] = {"wait_boot": bool(wait_boot)}
    if skip_home:
        payload["skip_home"] = True
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
