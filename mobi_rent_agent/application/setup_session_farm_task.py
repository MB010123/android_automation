"""Farm-side inspect / recovery / safe cleanup for in-app setup sessions.

ADB only. No EuiccManager, no Device Owner, no factory reset, no silent
eSIM delete, no privileged role assignment.
"""
from __future__ import annotations

import logging
import subprocess
from typing import Any

from application.farm_task_types import FarmTaskRequest, FarmTaskResult
from application.remote_access_farm_task import DCIM_CAMERA_DIR
from application.setup_activity_guard import (
    PHASE_VOIDFIX,
    decide_setup_guard,
    parse_foreground_activity,
    parse_sms_role_holders,
)
from infrastructure.adb_companion import AdbCommandError, AdbCommandRunner

logger = logging.getLogger("farm_agent.setup_session")

QR_PREFIX = "mobirent_esim_qr_"
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
    decision = decide_setup_guard(phase=phase, activity=activity, voidfix_package=voidfix_package)
    recovered = False
    if recover and not decision.allowed and decision.recover_intent:
        try:
            _shell(
                runner,
                adb_path,
                serial,
                ["am", "start", "-a", decision.recover_intent],
            )
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
    details = {
        "activity": activity,
        "allowed": bool(decision.allowed),
        "recovered": recovered,
        "reason": decision.reason,
        "phase": decision.phase,
        "sms_role_holder": sms_holder,
        "voidfix_is_default_sms": voidfix_is_sms,
        "voidfix_running": voidfix_running,
    }
    return FarmTaskResult(ok=True, http_status=200, details=details)


def run_setup_session_input(
    *,
    adb_path: str,
    slot_map: dict[int, str],
    request: FarmTaskRequest,
    command_runner: AdbCommandRunner | None = None,
) -> FarmTaskResult:
    """VPS-only fallback for swipe/nav when GADS has no matching endpoint."""
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
            args = [
                "input",
                "swipe",
                str(int(payload["x"])),
                str(int(payload["y"])),
                str(int(payload["x2"])),
                str(int(payload["y2"])),
            ]
            duration_ms = payload.get("duration_ms")
            if duration_ms is not None:
                args.append(str(int(duration_ms)))
            _shell(runner, adb_path, serial, args)
        elif kind == "tap":
            _shell(
                runner,
                adb_path,
                serial,
                ["input", "tap", str(int(payload["x"])), str(int(payload["y"]))],
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
        listing = _shell(runner, adb_path, serial, ["ls", DCIM_CAMERA_DIR])
        for name in (listing or "").split():
            if not name.startswith(QR_PREFIX):
                continue
            _shell(runner, adb_path, serial, ["rm", "-f", f"{DCIM_CAMERA_DIR}/{name}"])
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
