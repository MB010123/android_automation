"""Wake, unlock, and reset display size after reboot or when a live session starts.

Windows ``adb shell`` joins argv and drops quotes, so this sends one ``shell``
argument: ``cmd; true; cmd; true``. Individual Android denials never fail the
task. Never reboots. During eSIM handoff (``skip_home``) it wakes the panel,
never sends HOME, and brings OEM SIM/eSIM settings to the front.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Callable

from application.farm_task_types import FarmTaskRequest, FarmTaskResult
from application.esim_setup_ui_state import OEM_PIXEL, OEM_SAMSUNG, SETUP_UI_AWAITING
from application.device_hardware_lockdown import (
    ADB_UNRESPONSIVE_ERROR,
    USB_REPLUG_HINT,
    adb_shell_responsive,
    adb_shell_settled,
    apply_usb_popup_suppression,
    ensure_on_device_lockdown_guard,
    hardware_lockdown_commands,
    parse_admin_flag,
    set_device_admin_mode,
)
from application.remote_access_farm_task import (
    detect_device_oem,
    ensure_esim_setup_screen_open,
)
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


def _run_adb(runner: AdbCommandRunner, serial: str, arguments: list[str], timeout_seconds: float):
    try:
        return runner.run(serial, arguments, timeout_seconds=timeout_seconds)
    except TypeError:
        return runner.run(serial, arguments)


def _wait_boot_completed(
    runner: AdbCommandRunner,
    serial: str,
    warnings: list[str],
    sleep_fn: Callable[[float], None],
    timeout_seconds: float,
) -> bool:
    """Poll until ``sys.boot_completed=1``. Bound by wall clock, not N hung ADB calls.

    After a warm reboot a missing serial used to make each ``get-state`` hang
    for the full 20s runner timeout, so 90 polls blocked Farm Agent for tens
    of minutes and the Admin Control Panel live-access request never returned.
    """
    budget = max(1.0, float(timeout_seconds))
    poll_timeout = min(5.0, max(1.5, budget / 18.0))
    deadline = time.monotonic() + budget
    try:
        _run_adb(runner, serial, ["wait-for-device"], min(8.0, budget))
    except AdbCommandError as exc:
        warnings.append(f"wait-for-device: {exc.__class__.__name__}")
    while time.monotonic() < deadline:
        try:
            state = _run_adb(runner, serial, ["get-state"], poll_timeout).stdout.strip()
        except AdbCommandError:
            state = ""
        try:
            boot = _run_adb(
                runner, serial, ["shell", "getprop", "sys.boot_completed"], poll_timeout
            ).stdout.strip()
        except AdbCommandError:
            boot = ""
        if state == "device" and boot == "1":
            # S20 USB can appear in `adb devices` for a couple of seconds
            # with one live echo, then drop. Require consecutive echoes.
            remaining = deadline - time.monotonic()
            if adb_shell_settled(
                runner,
                serial,
                timeout_seconds=remaining,
                poll_seconds=BOOT_POLL_SECONDS,
                sleep_fn=sleep_fn,
                echo_timeout_seconds=poll_timeout,
            ):
                return True
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sleep_fn(min(BOOT_POLL_SECONDS, remaining))
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
    open_esim = skip_home or bool(request.payload.get("open_esim_settings", False))
    if open_esim or str(request.payload.get("setup_ui_state") or "").strip() == SETUP_UI_AWAITING:
        skip_home = True
        open_esim = True
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
                "hint": USB_REPLUG_HINT,
                "warnings": warnings,
            },
        )
    # `get-state` is answered by the host adb server; a phone whose adbd is
    # wedged (One UI after a bad USB re-init) still reports `device` while
    # every shell hangs. Probe once (5s) instead of 8 x 20s timeouts that make
    # the Admin Control Panel request time out.
    if not adb_shell_responsive(runner, serial):
        logger.warning("device_screen_prepare_adb_unresponsive slot=%s serial=%s", slot_id, serial)
        return FarmTaskResult(
            ok=False,
            http_status=409,
            error=ADB_UNRESPONSIVE_ERROR,
            details={
                "ok": False,
                "slot_id": slot_id,
                "serial": serial,
                "status": ADB_UNRESPONSIVE_ERROR,
                "hint": USB_REPLUG_HINT,
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
    oem = detect_device_oem(runner, serial, slot_id)
    if samsung or oem == OEM_SAMSUNG:
        oem = OEM_SAMSUNG
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
        hardware_lockdown_commands(),
        warnings,
        "hardware_lockdown",
    )
    try:
        if not ensure_on_device_lockdown_guard(runner, serial, redeploy=True):
            warnings.append("lockdown_guard: start_failed")
    except (AdbCommandError, OSError, TypeError) as exc:
        warnings.append(f"lockdown_guard: {exc.__class__.__name__}")
    # USB replug / reboot popups + ADB auth timer (settings + Samsung MTP
    # re-enable). Never reconfigures the USB gadget.
    try:
        if not apply_usb_popup_suppression(runner, serial, set_functions=False):
            warnings.append("usb_popup_suppression: skipped")
    except (AdbCommandError, OSError, TypeError) as exc:
        warnings.append(f"usb_popup_suppression: {exc.__class__.__name__}")
    admin = parse_admin_flag(request.payload.get("admin"))
    if admin is None:
        admin = False
    try:
        set_device_admin_mode(runner, serial, admin, ensure_guard=False)
    except (AdbCommandError, OSError, TypeError) as exc:
        warnings.append(f"access_mode: {exc.__class__.__name__}")
    esim_setup_launched = ""
    if open_esim:
        esim_setup_launched = ensure_esim_setup_screen_open(runner, serial, oem)
    package = str(request.payload.get("voidfix_package") or "").strip()
    if package:
        from application.device_voidfix_keepalive import apply_voidfix_keepalive, normalize_voidfix_package

        safe = normalize_voidfix_package(package)
        if safe:
            apply_voidfix_keepalive(runner, serial, safe, warnings, restart=False)
    if wait_boot and not adb_shell_responsive(runner, serial):
        logger.warning(
            "device_screen_prepare_dropped_after_boot slot=%s serial=%s",
            slot_id,
            serial,
        )
        return FarmTaskResult(
            ok=False,
            http_status=409,
            error=ADB_UNRESPONSIVE_ERROR,
            details={
                "ok": False,
                "slot_id": slot_id,
                "serial": serial,
                "status": ADB_UNRESPONSIVE_ERROR,
                "hint": USB_REPLUG_HINT,
                "warnings": warnings,
            },
        )
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
            "skip_home": skip_home,
            "admin": admin,
            "esim_setup_launched": esim_setup_launched,
            "setup_ui_state": SETUP_UI_AWAITING if open_esim else None,
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
    open_esim_settings: bool = False,
    admin: bool = False,
) -> None:
    """Best-effort VPS → Farm unlock. Never raises; never fails the caller."""
    if farm_client is None:
        return
    payload: dict[str, Any] = {"wait_boot": bool(wait_boot), "admin": bool(admin)}
    if skip_home:
        payload["skip_home"] = True
    if open_esim_settings or skip_home:
        payload["open_esim_settings"] = True
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
