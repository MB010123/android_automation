"""Farm Agent task: wipe renter data over ADB without reboot or eSIM changes.

Deletes user files, call log, contacts, SMS/MMS, and browser/cache state.
Never factory-resets, never reboots, never calls EuiccManager or deletes
profiles. Gallery (`com.sec.android.gallery3d`) and Settings stay enabled
so Samsung QR eSIM automation keeps working.

On stock unrooted Pixels/Samsungs, `content delete`, `pm clear`, and
`pm disable-user` often return non-zero. Those are warnings. The task
fails only when the bay is unknown or ADB `get-state` is not `device`.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from application.farm_task_types import FarmTaskRequest, FarmTaskResult
from domain.farm import MAX_FARM_SLOT_ID
from infrastructure.adb_companion import AdbCommandError, AdbCommandRunner

logger = logging.getLogger("farm_agent.device_data_cleanup")

DEVICE_DATA_CLEANUP_TASK = "device_data_cleanup"
CLEANUP_ADB_TIMEOUT_SECONDS = 45.0

# Wipe whole folders (no globs). `adb shell` joins argv with spaces and
# drops quotes, so `sh -c 'rm -rf …/*'` becomes `sh -c rm` → "Needs 1 argument".
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
)
# Must stay enabled: Samsung Gallery (QR pick) and Settings (manual eSIM).
PRESERVED_PACKAGES = frozenset({"com.sec.android.gallery3d", "com.android.settings"})


def _serial_for_slot(slot_map: dict[int, str], farm_slot_id: int) -> str | None:
    serial = str(slot_map.get(int(farm_slot_id)) or "").strip()
    return serial or None


def _short_error(exc: BaseException) -> str:
    text = str(exc).replace("\n", " ").strip()
    return text[:180] if text else exc.__class__.__name__


def _best_effort(
    runner: AdbCommandRunner,
    serial: str,
    arguments: list[str],
    warnings: list[str],
    label: str,
) -> bool:
    try:
        runner.run(serial, arguments)
        return True
    except AdbCommandError as exc:
        warnings.append(f"{label}: {_short_error(exc)}")
        logger.info("device_data_cleanup_step_skipped label=%s", label)
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
    cleared: list[str] = []

    files_ok = False
    for path in FILE_DIRS:
        removed = _best_effort(runner, mapped, ["shell", "rm", "-rf", path], warnings, f"rm {path}")
        restored = _best_effort(
            runner, mapped, ["shell", "mkdir", "-p", path], warnings, f"mkdir {path}"
        )
        files_ok = files_ok or removed or restored
    for uri in MEDIA_SCAN_URIS:
        _best_effort(
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
            warnings,
            f"media_scan {uri}",
        )
    if files_ok:
        cleared.append("files")

    if _best_effort(
        runner,
        mapped,
        ["shell", "content", "delete", "--uri", "content://call_log/calls"],
        warnings,
        "call_log",
    ):
        cleared.append("call_log")

    if _best_effort(
        runner,
        mapped,
        ["shell", "pm", "clear", "com.android.providers.contacts"],
        warnings,
        "contacts",
    ):
        cleared.append("contacts")

    sms_ok = _best_effort(
        runner, mapped, ["shell", "content", "delete", "--uri", "content://sms"], warnings, "sms"
    )
    mms_ok = _best_effort(
        runner, mapped, ["shell", "content", "delete", "--uri", "content://mms"], warnings, "mms"
    )
    for package in OPTIONAL_CLEAR_PACKAGES[:2]:
        _best_effort(runner, mapped, ["shell", "pm", "clear", package], warnings, f"pm clear {package}")
    if sms_ok or mms_ok:
        cleared.append("sms_mms")

    browsers_ok = False
    for package in OPTIONAL_CLEAR_PACKAGES[2:]:
        if _best_effort(
            runner, mapped, ["shell", "pm", "clear", package], warnings, f"pm clear {package}"
        ):
            browsers_ok = True
    _best_effort(
        runner,
        mapped,
        ["shell", "am", "force-stop", "com.android.settings"],
        warnings,
        "force-stop settings",
    )
    _best_effort(
        runner, mapped, ["shell", "input", "keyevent", "KEYCODE_HOME"], warnings, "home"
    )
    if browsers_ok:
        cleared.append("browsers")

    lockdown_applied = False
    if lockdown:
        lockdown_applied = True
        for package in LOCKDOWN_PACKAGES:
            if package in PRESERVED_PACKAGES:
                continue
            _best_effort(
                runner,
                mapped,
                ["shell", "pm", "disable-user", "--user", "0", package],
                warnings,
                f"lockdown {package}",
            )

    logger.info(
        "device_data_cleanup_ok slot=%s lockdown=%s cleared=%s warnings=%s",
        slot_id,
        lockdown_applied,
        ",".join(cleared),
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
        lockdown_applied=lockdown_applied,
        warnings=warnings,
    )
