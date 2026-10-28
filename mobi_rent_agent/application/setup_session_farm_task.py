"""Farm-side inspect / recovery / safe cleanup for in-app setup sessions.

ADB only. No EuiccManager, no Device Owner, no factory reset, no silent
eSIM delete, no privileged role assignment.
"""
from __future__ import annotations

import logging
import subprocess
from typing import Any

from application.device_display_size import map_touch_to_native, parse_wm_size
from application.farm_task_types import FarmTaskRequest, FarmTaskResult
from application.esim_setup_ui_state import (
    OEM_PIXEL,
    OEM_SAMSUNG,
    classify_setup_ui,
    public_setup_ui_body,
)
from application.remote_access_farm_task import (
    DCIM_CAMERA_DIR,
    DOWNLOAD_DIR,
    PICTURES_DIR,
    QR_PREFIX,
    QR_WALK_MARKER_PATH,
    _is_samsung_renter_device,
    delete_placed_esim_qr,
    launch_esim_setup_screen,
)
from application.samsung_one_ui import (
    hijack_escape_args,
    is_samsung_game_package,
    package_from_component,
    samsung_settings_args,
    samsung_setup_relaunch_args,
)
from application.setup_activity_guard import (
    PHASE_ESIM,
    PHASE_VOIDFIX,
    decide_setup_guard,
    parse_foreground_activity,
    parse_sms_role_holders,
    should_recover_setup,
)
from infrastructure.adb_companion import AdbCommandError, AdbCommandRunner

logger = logging.getLogger("farm_agent.setup_session")

SYSTEM_SMS_PACKAGES = frozenset(
    {
        "com.google.android.apps.messaging",
        "com.android.mms",
        "com.android.messaging",
        "com.samsung.android.messaging",
        "com.google.android.gms",
    }
)


def _serial(slot_map: dict[int, str], farm_slot_id: int) -> str | None:
    serial = slot_map.get(int(farm_slot_id))
    if not serial or not str(serial).strip():
        return None
    return str(serial).strip()


def _shell(
    runner: AdbCommandRunner | None,
    adb_path: str,
    serial: str,
    arguments: list[str],
) -> str:
    if runner is not None:
        return runner.run(serial, ["shell", *arguments]).stdout or ""
    completed = subprocess.run(
        [adb_path, "-s", serial, "shell", *arguments],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return completed.stdout or ""


def _live_wm_size(
    runner: AdbCommandRunner | None,
    adb_path: str,
    serial: str,
) -> tuple[int, int] | None:
    try:
        return parse_wm_size(_shell(runner, adb_path, serial, ["wm", "size"]))
    except (AdbCommandError, OSError, subprocess.TimeoutExpired):
        return None


def _native_tap_point(
    runner: AdbCommandRunner | None,
    adb_path: str,
    serial: str,
    payload: dict[str, Any],
) -> tuple[int, int]:
    size = _live_wm_size(runner, adb_path, serial)
    if size is not None:
        mapped = map_touch_to_native(payload["x"], payload["y"], size[0], size[1])
        if mapped is not None:
            return mapped
    return int(payload["x"]), int(payload["y"])


def _native_swipe_points(
    runner: AdbCommandRunner | None,
    adb_path: str,
    serial: str,
    payload: dict[str, Any],
) -> tuple[int, int, int, int]:
    size = _live_wm_size(runner, adb_path, serial)
    if size is not None:
        start = map_touch_to_native(payload["x"], payload["y"], size[0], size[1])
        end = map_touch_to_native(payload["x2"], payload["y2"], size[0], size[1])
        if start is not None and end is not None:
            return start[0], start[1], end[0], end[1]
    return int(payload["x"]), int(payload["y"]), int(payload["x2"]), int(payload["y2"])


def run_setup_session_inspect(
    *,
    adb_path: str,
    slot_map: dict[int, str],
    request: FarmTaskRequest,
    command_runner: AdbCommandRunner | None = None,
) -> FarmTaskResult:
    serial = _serial(slot_map, request.farm_slot_id)
    if not serial:
        return FarmTaskResult(ok=False, http_status=404, error="slot_not_found")
    payload = request.payload if isinstance(request.payload, dict) else {}
    phase = str(payload.get("phase") or "esim").strip().lower()
    voidfix_package = str(payload.get("voidfix_package") or "").strip() or None
    recover = bool(payload.get("recover", False))
    qr_walk_completed = bool(payload.get("qr_walk_completed", False))
    esim_profile_present = bool(payload.get("esim_profile_present", False))
    activation_confirmed = bool(payload.get("activation_confirmed", False))
    runner = command_runner
    try:
        activities = _shell(runner, adb_path, serial, ["dumpsys", "activity", "activities"])
        role_dump = _shell(runner, adb_path, serial, ["dumpsys", "role"])
    except (AdbCommandError, OSError, subprocess.TimeoutExpired):
        logger.warning("setup_session_inspect_adb_failed slot=%s", request.farm_slot_id)
        return FarmTaskResult(
            ok=False,
            http_status=422,
            error="inspect_failed",
            details={"allowed": False, "reason": "adb_failed"},
        )
    activity = parse_foreground_activity(activities)
    try:
        from application.device_hardware_lockdown import reassert_audio_lockdown

        lock_runner = runner or AdbCommandRunner(adb_path, timeout_seconds=8.0)
        reassert_audio_lockdown(lock_runner, serial, activity=activity, force=True)
    except Exception:
        logger.info("audio_lockdown_inspect_skipped slot=%s", request.farm_slot_id)
    decision = decide_setup_guard(phase=phase, activity=activity, voidfix_package=voidfix_package)
    recovered = False
    samsung = False
    try:
        manufacturer = _shell(runner, adb_path, serial, ["getprop", "ro.product.manufacturer"]).strip()
        samsung = _is_samsung_renter_device(int(request.farm_slot_id), serial, manufacturer)
    except (AdbCommandError, OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        samsung = _is_samsung_renter_device(int(request.farm_slot_id), serial, None)
    hijacked = decision.reason == "hijacked_package" or is_samsung_game_package(
        package_from_component(activity)
    )
    recover_setup = should_recover_setup(
        allowed=decision.allowed,
        qr_walk_completed=qr_walk_completed,
        esim_profile_present=esim_profile_present,
        activation_confirmed=activation_confirmed,
    )
    if hijacked:
        try:
            for args in hijack_escape_args(activity):
                _shell(runner, adb_path, serial, args)
            recovered = True
        except (AdbCommandError, OSError, subprocess.TimeoutExpired):
            recovered = False
    if (recover and recover_setup) or (hijacked and samsung and recover_setup):
        recover_args: list[str] | None = None
        if samsung:
            recover_args = samsung_setup_relaunch_args(phase=phase)
        elif decision.recover_intent:
            recover_args = ["am", "start", "-a", decision.recover_intent]
        if recover_args:
            try:
                if not hijacked and activity and not decision.allowed:
                    current_pkg = package_from_component(activity)
                    if (
                        current_pkg
                        and current_pkg not in {
                            "com.android.settings",
                            "com.samsung.android.app.telephonyui",
                        }
                        and "launcher" not in current_pkg.lower()
                    ):
                        _shell(runner, adb_path, serial, ["am", "force-stop", current_pkg])
                if runner is not None and phase == PHASE_ESIM:
                    launch_esim_setup_screen(
                        runner, serial, OEM_SAMSUNG if samsung else OEM_PIXEL
                    )
                else:
                    _shell(runner, adb_path, serial, recover_args)
                recovered = True
                activities = _shell(runner, adb_path, serial, ["dumpsys", "activity", "activities"])
                activity = parse_foreground_activity(activities)
                decision = decide_setup_guard(
                    phase=phase, activity=activity, voidfix_package=voidfix_package
                )
            except (AdbCommandError, OSError, subprocess.TimeoutExpired):
                recovered = False
    holders = parse_sms_role_holders(role_dump)
    sms_holder = holders[0] if holders else None
    voidfix_is_sms = bool(voidfix_package and sms_holder == voidfix_package)
    voidfix_running = False
    if voidfix_package:
        try:
            services = _shell(
                runner, adb_path, serial, ["dumpsys", "activity", "services", voidfix_package]
            )
            voidfix_running = voidfix_package in (services or "")
        except (AdbCommandError, OSError, subprocess.TimeoutExpired):
            voidfix_running = False
    if not qr_walk_completed:
        qr_walk_completed = _marker_says_walk_done(
            runner, adb_path, serial, str(payload.get("qr_sha256") or "")
        )
    # Never uiautomator-dump during eSIM setup: the dump interrupts GADS
    # MediaProjection and drops the remote stream after QR handoff.
    classification = classify_setup_ui(
        None,
        activity,
        walk_completed=qr_walk_completed,
        walk_in_progress=bool(payload.get("walk_in_progress", False)),
        activation_confirmed=activation_confirmed,
        esim_profile_present=esim_profile_present,
        oem=OEM_SAMSUNG if samsung else OEM_PIXEL,
    )
    qr_deleted: list[str] = []
    consume_qr = (
        esim_profile_present
        or activation_confirmed
        or classification.state == "completed"
        or classification.reason in {"success_copy", "esim_installed"}
    )
    if consume_qr:
        qr_deleted = _delete_qr_files(runner, adb_path, serial)
    if classification.state == "completed":
        try:
            _shell(runner, adb_path, serial, ["input", "keyevent", "3"])
        except (AdbCommandError, OSError, subprocess.TimeoutExpired):
            pass
    details = {
        "activity": activity,
        "allowed": bool(decision.allowed),
        "recovered": recovered,
        "reason": decision.reason,
        "phase": decision.phase,
        "sms_role_holder": sms_holder,
        "voidfix_is_default_sms": voidfix_is_sms,
        "voidfix_running": voidfix_running,
        "qr_walk_completed": qr_walk_completed or classification.state == "completed",
        "qr_deleted": bool(qr_deleted),
        **public_setup_ui_body(classification),
    }
    return FarmTaskResult(ok=True, http_status=200, details=details)


def _marker_says_walk_done(
    runner: AdbCommandRunner | None,
    adb_path: str,
    serial: str,
    qr_sha256: str,
) -> bool:
    try:
        text = _shell(runner, adb_path, serial, ["cat", QR_WALK_MARKER_PATH]).strip()
    except (AdbCommandError, OSError, subprocess.TimeoutExpired):
        return False
    if not text:
        return False
    sha = str(qr_sha256 or "").strip().lower()
    if sha:
        return sha in text.lower()
    return True


def _delete_qr_files(
    runner: AdbCommandRunner | None,
    adb_path: str,
    serial: str,
) -> list[str]:
    if runner is not None:
        try:
            return delete_placed_esim_qr(runner, serial)
        except AdbCommandError:
            return []
    removed: list[str] = []
    try:
        from application.remote_access_farm_task import PLACED_ESIM_QR_PATHS

        for path in PLACED_ESIM_QR_PATHS:
            _shell(runner, adb_path, serial, ["rm", "-f", path])
            removed.append(path)
        for directory in (PICTURES_DIR, DOWNLOAD_DIR, DCIM_CAMERA_DIR, "/sdcard/DCIM"):
            listing = _shell(runner, adb_path, serial, ["ls", directory])
            for name in (listing or "").split():
                if name.startswith(QR_PREFIX):
                    _shell(runner, adb_path, serial, ["rm", "-f", f"{directory}/{name}"])
                    removed.append(f"{directory}/{name}")
    except (AdbCommandError, OSError, subprocess.TimeoutExpired):
        return removed
    return removed


def run_setup_session_input(
    *,
    adb_path: str,
    slot_map: dict[int, str],
    request: FarmTaskRequest,
    command_runner: AdbCommandRunner | None = None,
) -> FarmTaskResult:
    """VPS-only fallback for tap/swipe/nav when GADS has no matching endpoint."""
    serial = _serial(slot_map, request.farm_slot_id)
    if not serial:
        return FarmTaskResult(ok=False, http_status=404, error="slot_not_found")
    payload = request.payload if isinstance(request.payload, dict) else {}
    kind = str(payload.get("kind") or "").strip().lower()
    runner = command_runner
    try:
        if kind == "back":
            _shell(runner, adb_path, serial, ["input", "keyevent", "4"])
        elif kind == "home":
            _shell(runner, adb_path, serial, ["input", "keyevent", "3"])
        elif kind == "recents":
            _shell(runner, adb_path, serial, ["input", "keyevent", "187"])
        elif kind == "notification_shade":
            _shell(runner, adb_path, serial, ["cmd", "statusbar", "expand-notifications"])
        elif kind == "quick_settings":
            _shell(runner, adb_path, serial, ["cmd", "statusbar", "expand-settings"])
        elif kind == "swipe":
            x, y, x2, y2 = _native_swipe_points(runner, adb_path, serial, payload)
            args = ["input", "swipe", str(x), str(y), str(x2), str(y2)]
            duration_ms = payload.get("duration_ms")
            if duration_ms is not None:
                args.append(str(int(duration_ms)))
            _shell(runner, adb_path, serial, args)
        elif kind == "tap":
            x, y = _native_tap_point(runner, adb_path, serial, payload)
            _shell(runner, adb_path, serial, ["input", "tap", str(x), str(y)])
        elif kind in {"settings", "android_settings"}:
            samsung = False
            try:
                manufacturer = _shell(
                    runner, adb_path, serial, ["getprop", "ro.product.manufacturer"]
                ).strip()
                samsung = _is_samsung_renter_device(int(request.farm_slot_id), serial, manufacturer)
            except (AdbCommandError, OSError, subprocess.TimeoutExpired, TypeError, ValueError):
                samsung = _is_samsung_renter_device(int(request.farm_slot_id), serial, None)
            if samsung:
                _shell(runner, adb_path, serial, samsung_settings_args())
            else:
                _shell(
                    runner,
                    adb_path,
                    serial,
                    ["am", "start", "-a", "android.settings.SETTINGS", "--activity-clear-task"],
                )
        else:
            return FarmTaskResult(ok=False, http_status=400, error="invalid_control")
    except (AdbCommandError, OSError, subprocess.TimeoutExpired, KeyError, TypeError, ValueError):
        return FarmTaskResult(ok=False, http_status=422, error="input_failed")
    return FarmTaskResult(ok=True, http_status=200)


def run_setup_session_voidfix_cycle(
    *,
    adb_path: str,
    slot_map: dict[int, str],
    request: FarmTaskRequest,
    command_runner: AdbCommandRunner | None = None,
) -> FarmTaskResult:
    """Restart the VoidFix app process after the customer granted default SMS.

    Does not assign ROLE_SMS. If VoidFix is not already the holder, this fails.
    """
    serial = _serial(slot_map, request.farm_slot_id)
    if not serial:
        return FarmTaskResult(ok=False, http_status=404, error="slot_not_found")
    payload = request.payload if isinstance(request.payload, dict) else {}
    package = str(payload.get("voidfix_package") or "").strip()
    if not package or package in SYSTEM_SMS_PACKAGES:
        return FarmTaskResult(
            ok=False,
            http_status=422,
            error="voidfix_package_unconfigured",
            details={"cycled": False, "reason": "package_unconfigured"},
        )
    runner = command_runner
    try:
        role_dump = _shell(runner, adb_path, serial, ["dumpsys", "role"])
        holders = parse_sms_role_holders(role_dump)
        if package not in holders:
            return FarmTaskResult(
                ok=True,
                http_status=200,
                details={
                    "cycled": False,
                    "voidfix_is_default_sms": False,
                    "reason": "customer_sms_approval_required",
                    "sms_role_holder": holders[0] if holders else None,
                },
            )
        _shell(runner, adb_path, serial, ["am", "force-stop", package])
        _shell(
            runner,
            adb_path,
            serial,
            ["monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"],
        )
        services = _shell(runner, adb_path, serial, ["dumpsys", "activity", "services", package])
        running = package in (services or "")
        return FarmTaskResult(
            ok=True,
            http_status=200,
            details={
                "cycled": True,
                "voidfix_is_default_sms": True,
                "voidfix_running": running,
                "sms_role_holder": package,
            },
        )
    except (AdbCommandError, OSError, subprocess.TimeoutExpired):
        return FarmTaskResult(ok=False, http_status=422, error="voidfix_cycle_failed")


def run_setup_session_safe_cleanup(
    *,
    adb_path: str,
    slot_map: dict[int, str],
    request: FarmTaskRequest,
    command_runner: AdbCommandRunner | None = None,
) -> FarmTaskResult:
    """Delete only Mobi-Rent QR artifacts we placed. Never wipe or delete eSIM."""
    serial = _serial(slot_map, request.farm_slot_id)
    if not serial:
        return FarmTaskResult(ok=False, http_status=404, error="slot_not_found")
    runner = command_runner
    removed = False
    try:
        if runner is not None:
            deleted = delete_placed_esim_qr(runner, serial)
            removed = bool(deleted)
        else:
            for directory in (DCIM_CAMERA_DIR, PICTURES_DIR, DOWNLOAD_DIR, "/sdcard/DCIM"):
                listing = _shell(runner, adb_path, serial, ["ls", directory])
                for name in (listing or "").split():
                    if not name.startswith(QR_PREFIX) and name not in (
                        "esim_qr.png",
                        "esim_qr.jpg",
                        "esim_qr.jpeg",
                        "esim_qr.webp",
                    ):
                        continue
                    _shell(runner, adb_path, serial, ["rm", "-f", f"{directory}/{name}"])
                    removed = True
    except (AdbCommandError, OSError, subprocess.TimeoutExpired):
        logger.warning("setup_session_safe_cleanup_failed slot=%s", request.farm_slot_id)
        return FarmTaskResult(
            ok=False,
            http_status=422,
            error="cleanup_failed",
            details={"removed_qr_artifacts": False},
        )
    return FarmTaskResult(
        ok=True,
        http_status=200,
        details={"removed_qr_artifacts": removed, "factory_reset": False, "esim_deleted": False},
    )
