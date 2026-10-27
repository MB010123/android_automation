"""Farm Agent task: wipe renter data over ADB.

``preserve_esim=true`` (default, "Clean Phone (Keep eSIM)"): delete user files,
call log, contacts, SMS/MMS, accounts, launcher data, and browser/cache.
Never factory-resets, never reboots, never calls EuiccManager or formats the
eUICC partition. Gallery and Settings stay enabled so Samsung QR eSIM
automation keeps working.

``preserve_esim=false`` ("Full Wipe & Delete eSIM"): request a platform factory
reset with ``WIPE_ESIMS`` (not EuiccManager), then ``adb reboot recovery``.
After the device returns, re-apply lockdown. Never silent LPA/EuiccManager.

One ``adb -s <serial> shell`` script (``cmd; true`` per step) so Windows
does not spawn 15 ADB processes or fail on unrooted denials. Keep-eSIM
fails only when the bay is unknown or ADB ``get-state`` is not ``device``.
This task has no rental/reservation gate; the VPS admin clean endpoints may
run it on an active rental.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from application.device_screen_prepare import (
    BOOT_WAIT_TIMEOUT_SECONDS,
    WAKE_UNLOCK_COMMANDS,
    _wait_boot_completed,
    run_best_effort_shell,
)
from application.farm_task_types import FarmTaskRequest, FarmTaskResult
from domain.farm import MAX_FARM_SLOT_ID
from infrastructure.adb_companion import AdbCommandError, AdbCommandRunner

logger = logging.getLogger("farm_agent.device_data_cleanup")

DEVICE_DATA_CLEANUP_TASK = "device_data_cleanup"
CLEANUP_ADB_TIMEOUT_SECONDS = 20.0
CLEANUP_WIPE_TIMEOUT_SECONDS = 180.0

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
    "com.samsung.android.game.gamehome",
    "com.samsung.android.game.gametools",
    "com.samsung.android.game.gos",
    "com.enhance.gameservice",
)
PRESERVED_PACKAGES = frozenset({"com.sec.android.gallery3d", "com.android.settings"})
# Never pm-clear or disable these; they own carrier/eSIM state.
ESIM_PROTECTED_PACKAGES = frozenset(
    {
        "com.android.euicc",
        "com.google.android.euicc",
        "com.samsung.android.app.telephonyui",
        "com.android.phone",
        "com.android.stk",
        "com.samsung.android.incallui",
    }
)
ACCOUNT_CLEAR_PACKAGES = (
    "com.osp.app.signin",
    "com.samsung.android.mobileservice",
    "com.google.android.gm",
    "com.samsung.android.email.provider",
    "com.google.android.apps.googlequicksearchbox",
)
LAUNCHER_CLEAR_PACKAGES = (
    "com.sec.android.app.launcher",
    "com.google.android.apps.nexuslauncher",
    "com.android.launcher3",
)
FACTORY_RESET_COMMANDS = (
    "am broadcast -a android.intent.action.FACTORY_RESET "
    "-n android/com.android.server.MasterClearReceiver "
    "--es android.intent.extra.REASON MasterClearConfirm "
    "--ez android.intent.extra.WIPE_EXTERNAL_STORAGE true "
    "--ez android.intent.extra.WIPE_ESIMS true",
    "am broadcast -a android.intent.action.MASTER_CLEAR",
    "mkdir -p /cache/recovery",
    "echo --wipe_data > /cache/recovery/command",
    "echo --wipe_cache >> /cache/recovery/command",
)


def parse_preserve_esim(payload: dict[str, Any] | None) -> bool:
    """Default keep-eSIM. Explicit false/0/no/off requests a full wipe."""
    if not isinstance(payload, dict) or "preserve_esim" not in payload:
        return True
    value = payload.get("preserve_esim")
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"0", "false", "no", "off"}:
            return False
        if text in {"1", "true", "yes", "on"}:
            return True
        return True
    if value is None:
        return True
    return bool(value)


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
        if package in ESIM_PROTECTED_PACKAGES:
            continue
        commands.append(f"pm clear {package}")
    for package in ACCOUNT_CLEAR_PACKAGES:
        if package in ESIM_PROTECTED_PACKAGES:
            continue
        commands.append(f"pm clear {package}")
    for package in LAUNCHER_CLEAR_PACKAGES:
        if package in ESIM_PROTECTED_PACKAGES:
            continue
        commands.append(f"pm clear {package}")
    commands.append("am force-stop com.android.settings")
    if lockdown:
        for package in LOCKDOWN_PACKAGES:
            if package in PRESERVED_PACKAGES or package in ESIM_PROTECTED_PACKAGES:
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
        "esim_preserved": details.get("esim_preserved", True),
        "rebooted": details.get("rebooted", False),
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
    sleeper = sleep_fn or __import__("time").sleep
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
    preserve_esim = parse_preserve_esim(request.payload)
    runner = command_runner or AdbCommandRunner(
        adb_path,
        timeout_seconds=(
            CLEANUP_WIPE_TIMEOUT_SECONDS if not preserve_esim else CLEANUP_ADB_TIMEOUT_SECONDS
        ),
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
            esim_preserved=preserve_esim,
            rebooted=False,
        )

    warnings: list[str] = []
    rebooted = False
    if not preserve_esim:
        run_best_effort_shell(
            runner, mapped, FACTORY_RESET_COMMANDS, warnings, "factory_reset"
        )
        try:
            runner.run(mapped, ["reboot", "recovery"])
            rebooted = True
        except AdbCommandError as exc:
            rebooted = True
            warnings.append(f"reboot_recovery: {exc.__class__.__name__}")
        online = _wait_boot_completed(
            runner, mapped, warnings, sleeper, BOOT_WAIT_TIMEOUT_SECONDS
        )
        if not online:
            logger.warning("device_data_cleanup_wipe_offline slot=%s", slot_id)
            return _result(
                ok=False,
                http_status=409,
                job_id=request.job_id,
                slot_id=slot_id,
                serial=mapped,
                error="device_offline",
                status="cleanup_failed",
                esim_preserved=False,
                rebooted=rebooted,
                warnings=warnings,
            )

    commands = cleanup_shell_commands(lockdown=lockdown)
    script_ok = run_best_effort_shell(
        runner, mapped, commands, warnings, "cleanup_script"
    )
    cleared = ["files", "call_log", "contacts", "sms_mms", "browsers", "accounts", "launchers"]
    logger.info(
        "device_data_cleanup_ok slot=%s lockdown=%s preserve_esim=%s script_ok=%s warnings=%s",
        slot_id,
        lockdown,
        preserve_esim,
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
        esim_preserved=preserve_esim,
        rebooted=rebooted,
        warnings=warnings,
    )
