"""Farm Agent task: wipe renter data over ADB without reboot or eSIM changes.

Deletes user files, call log, contacts, SMS/MMS, and browser/cache state.
Never factory-resets, never reboots, never calls EuiccManager or deletes
profiles. Gallery (`com.sec.android.gallery3d`) and Settings stay enabled
so Samsung QR eSIM automation keeps working.

One ``adb -s <serial> shell`` script (``cmd; true`` per step) so Windows
does not spawn 15 ADB processes or fail on unrooted denials. The task
fails only when the bay is unknown or ADB ``get-state`` is not ``device``.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from application.device_screen_prepare import WAKE_UNLOCK_COMMANDS, run_best_effort_shell
from application.farm_task_types import FarmTaskRequest, FarmTaskResult
from domain.farm import MAX_FARM_SLOT_ID
from infrastructure.adb_companion import AdbCommandError, AdbCommandRunner

logger = logging.getLogger("farm_agent.device_data_cleanup")

DEVICE_DATA_CLEANUP_TASK = "device_data_cleanup"
CLEANUP_ADB_TIMEOUT_SECONDS = 20.0

FILE_DIRS = (
    "/sdcard/Download",
    "/sdcard/Pictures",
    "/sdcard/DCIM/Camera",
    "/sdcard/DCIM/Screenshots",
    "/sdcard/Documents",
)
MEDIA_SCAN_URIS = ("file:///sdcard/Pictures", "file:///sdcard/DCIM")
OPTIONAL_CLEAR_PACKAGES = (
    "com.google.android.apps.messaging",
    "com.samsung.android.messaging",
    "com.android.chrome",
    "com.sec.android.app.sbrowser",
)
LOCKDOWN_PACKAGES = (
    "com.android.vending",
    "com.sec.android.app.samsungapps",
    "com.android.chrome",
    "com.sec.android.app.sbrowser",
    "com.google.android.documentsui",
    "com.sec.android.app.myfiles",
    "com.netflix.mediaclient",
    "com.netflix.partner.activation",
    "com.samsung.android.app.spage",
    "com.sec.android.app.kidshome",
)
PRESERVED_PACKAGES = frozenset({"com.sec.android.gallery3d", "com.android.settings"})


def _serial_for_slot(slot_map: dict[int, str], farm_slot_id: int) -> str | None:
    serial = str(slot_map.get(int(farm_slot_id)) or "").strip()
    return serial or None


def cleanup_shell_commands(*, lockdown: bool) -> list[str]:
    commands = list(WAKE_UNLOCK_COMMANDS)
    for path in FILE_DIRS:
        commands.append(f"rm -rf {path}")
        commands.append(f"mkdir -p {path}")
    for uri in MEDIA_SCAN_URIS:
        commands.append(
            "am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE "
            f"-d {uri}"
        )
    commands.append("content delete --uri content://call_log/calls")
    commands.append("pm clear com.android.providers.contacts")
    commands.append("content delete --uri content://sms")
    commands.append("content delete --uri content://mms")
    for package in OPTIONAL_CLEAR_PACKAGES:
        commands.append(f"pm clear {package}")
    commands.append("am force-stop com.android.settings")
    if lockdown:
        for package in LOCKDOWN_PACKAGES:
            if package in PRESERVED_PACKAGES:
                continue
            commands.append(f"pm disable-user --user 0 {package}")
    return commands


def _result(
    *,
    ok: bool,
    http_status: int,
    job_id: str,
    slot_id: int,
    serial: str | None,
    error: str | None = None,
    **details: Any,
) -> FarmTaskResult:
    body: dict[str, Any] = {
        "ok": ok,
        "job_id": job_id,
        "slot_id": int(slot_id),
        "serial": serial,
        "esim_preserved": True,
        "rebooted": False,
        "error_code": error,
    }
    body.update(details)
    return FarmTaskResult(
        ok=ok,
        http_status=http_status,
        error=error,
        details=body,
    )


def run_device_data_cleanup(
    *,
    adb_path: str,
    slot_map: dict[int, str],
    request: FarmTaskRequest,
    command_runner: AdbCommandRunner | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> FarmTaskResult:
    del sleep_fn
    slot_id = int(request.farm_slot_id)
    if not 1 <= slot_id <= MAX_FARM_SLOT_ID:
        return _result(
            ok=False,
            http_status=404,
            job_id=request.job_id,
            slot_id=slot_id,
            serial=None,
            error="slot_not_found",
        )
    mapped = _serial_for_slot(slot_map, slot_id)
    if not mapped:
        return _result(
            ok=False,
            http_status=404,
            job_id=request.job_id,
            slot_id=slot_id,
            serial=None,
            error="slot_not_found",
        )
    claimed = str(request.payload.get("serial") or "").strip()
    if claimed and claimed != mapped:
        return _result(
            ok=False,
            http_status=409,
            job_id=request.job_id,
            slot_id=slot_id,
            serial=mapped,
            error="serial_mismatch",
        )
    lockdown = bool(request.payload.get("lockdown", True))
    runner = command_runner or AdbCommandRunner(
        adb_path, timeout_seconds=CLEANUP_ADB_TIMEOUT_SECONDS
    )
    try:
        state = runner.run(mapped, ["get-state"]).stdout
    except AdbCommandError:
        state = None
    if state != "device":
        return _result(
            ok=False,
            http_status=409,
            job_id=request.job_id,
            slot_id=slot_id,
            serial=mapped,
            error="device_offline",
        )

    warnings: list[str] = []
    commands = cleanup_shell_commands(lockdown=lockdown)
    script_ok = run_best_effort_shell(
        runner, mapped, commands, warnings, "cleanup_script"
    )
    cleared = ["files", "call_log", "contacts", "sms_mms", "browsers"]
    logger.info(
        "device_data_cleanup_ok slot=%s lockdown=%s script_ok=%s warnings=%s",
        slot_id,
        lockdown,
        script_ok,
        len(warnings),
    )
    return _result(
        ok=True,
        http_status=200,
        job_id=request.job_id,
        slot_id=slot_id,
        serial=mapped,
        status="cleanup_completed",
        cleared=cleared,
        lockdown_applied=lockdown,
        warnings=warnings,
    )
