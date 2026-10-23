"""Farm Agent task: wipe renter data over ADB without reboot or eSIM changes.

Deletes user files, call log, contacts, SMS/MMS, and browser/cache state.
Never factory-resets, never reboots, never calls EuiccManager or deletes
profiles. Gallery (`com.sec.android.gallery3d`) and Settings stay enabled
so Samsung QR eSIM automation keeps working.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from application.farm_task_types import FarmTaskRequest, FarmTaskResult
from domain.farm import MAX_FARM_SLOT_ID
from infrastructure.adb_companion import AdbCommandError, AdbCommandRunner

logger = logging.getLogger("farm_agent.device_data_cleanup")

DEVICE_DATA_CLEANUP_TASK = "device_data_cleanup"

FILE_PATHS = (
    "/sdcard/Download/*",
    "/sdcard/Pictures/*",
    "/sdcard/DCIM/Camera/*",
    "/sdcard/DCIM/Screenshots/*",
    "/sdcard/Documents/*",
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
)
# Must stay enabled: Samsung Gallery (QR pick) and Settings (manual eSIM).
PRESERVED_PACKAGES = frozenset({"com.sec.android.gallery3d", "com.android.settings"})


def _serial_for_slot(slot_map: dict[int, str], farm_slot_id: int) -> str | None:
    serial = str(slot_map.get(int(farm_slot_id)) or "").strip()
    return serial or None


def _optional_shell(runner: AdbCommandRunner, serial: str, arguments: list[str]) -> bool:
    try:
        runner.run(serial, arguments)
        return True
    except AdbCommandError:
        return False


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
    runner = command_runner or AdbCommandRunner(adb_path)
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

    cleared: list[str] = []
    try:
        runner.run(mapped, ["shell", "sh", "-c", "rm -rf " + " ".join(FILE_PATHS)])
        for uri in MEDIA_SCAN_URIS:
            _optional_shell(
                runner,
                mapped,
                [
                    "shell",
                    "am",
                    "broadcast",
                    "-a",
                    "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                    "-d",
                    uri,
                ],
            )
        cleared.append("files")

        runner.run(mapped, ["shell", "content", "delete", "--uri", "content://call_log/calls"])
        cleared.append("call_log")

        runner.run(mapped, ["shell", "pm", "clear", "com.android.providers.contacts"])
        cleared.append("contacts")

        sms_ok = _optional_shell(
            runner, mapped, ["shell", "content", "delete", "--uri", "content://sms"]
        )
        mms_ok = _optional_shell(
            runner, mapped, ["shell", "content", "delete", "--uri", "content://mms"]
        )
        for package in OPTIONAL_CLEAR_PACKAGES[:2]:
            _optional_shell(runner, mapped, ["shell", "pm", "clear", package])
        if sms_ok or mms_ok:
            cleared.append("sms_mms")

        for package in OPTIONAL_CLEAR_PACKAGES[2:]:
            _optional_shell(runner, mapped, ["shell", "pm", "clear", package])
        _optional_shell(runner, mapped, ["shell", "am", "force-stop", "com.android.settings"])
        _optional_shell(runner, mapped, ["shell", "input", "keyevent", "KEYCODE_HOME"])
        cleared.append("browsers")
    except AdbCommandError:
        logger.warning("device_data_cleanup_adb_failed slot=%s", slot_id)
        return _result(
            ok=False,
            http_status=422,
            job_id=request.job_id,
            slot_id=slot_id,
            serial=mapped,
            error="cleanup_adb_failed",
            status="cleanup_failed",
            cleared=cleared,
            lockdown_applied=False,
        )

    lockdown_applied = False
    if lockdown:
        lockdown_applied = True
        for package in LOCKDOWN_PACKAGES:
            if package in PRESERVED_PACKAGES:
                continue
            _optional_shell(
                runner,
                mapped,
                ["shell", "pm", "disable-user", "--user", "0", package],
            )

    logger.info(
        "device_data_cleanup_ok slot=%s lockdown=%s cleared=%s",
        slot_id,
        lockdown_applied,
        ",".join(cleared),
    )
    return _result(
        ok=True,
        http_status=200,
        job_id=request.job_id,
        slot_id=slot_id,
        serial=mapped,
        status="cleanup_completed",
        cleared=cleared,
        lockdown_applied=lockdown_applied,
    )
