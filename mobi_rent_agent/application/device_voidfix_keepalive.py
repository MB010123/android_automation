"""Keep VoidFix alive across battery optimization and offer a repair restart."""
from __future__ import annotations

import logging
import re
from typing import Any

from application.device_screen_prepare import run_best_effort_shell
from application.farm_task_types import FarmTaskRequest, FarmTaskResult
from domain.farm import MAX_FARM_SLOT_ID
from infrastructure.adb_companion import AdbCommandError, AdbCommandRunner

logger = logging.getLogger("farm_agent.voidfix_keepalive")

VOIDFIX_REPAIR_TASK = "voidfix_repair"
_SAFE_PACKAGE = re.compile(r"^[A-Za-z0-9._]+$")


def voidfix_keepalive_commands(package: str) -> tuple[str, ...]:
    return (
        f"cmd appops set {package} RUN_IN_BACKGROUND allow",
        f"dumpsys deviceidle whitelist +{package}",
    )


def normalize_voidfix_package(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or not _SAFE_PACKAGE.match(text):
        return None
    return text


def _serial_for_slot(slot_map: dict[int, str], farm_slot_id: int) -> str | None:
    serial = str(slot_map.get(int(farm_slot_id)) or "").strip()
    return serial or None


def _package_running(runner: AdbCommandRunner, serial: str, package: str) -> bool:
    try:
        out = runner.run(serial, ["shell", f"pidof {package}"]).stdout.strip()
    except AdbCommandError:
        return False
    return bool(out and out != "0")


def apply_voidfix_keepalive(
    runner: AdbCommandRunner,
    serial: str,
    package: str,
    warnings: list[str],
    *,
    restart: bool = False,
) -> dict[str, Any]:
    run_best_effort_shell(
        runner,
        serial,
        list(voidfix_keepalive_commands(package)),
        warnings,
        "voidfix_keepalive",
    )
    running = _package_running(runner, serial, package)
    restarted = False
    if restart or not running:
        run_best_effort_shell(
            runner,
            serial,
            [
                f"monkey -p {package} -c android.intent.category.LAUNCHER 1",
            ],
            warnings,
            "voidfix_restart",
        )
        restarted = True
        running = _package_running(runner, serial, package)
    return {"package": package, "running": running, "restarted": restarted}


def run_voidfix_repair(
    *,
    adb_path: str,
    slot_map: dict[int, str],
    request: FarmTaskRequest,
    command_runner: AdbCommandRunner | None = None,
    voidfix_package: str | None = None,
) -> FarmTaskResult:
    slot_id = int(request.farm_slot_id)
    if not 1 <= slot_id <= MAX_FARM_SLOT_ID:
        return FarmTaskResult(ok=False, http_status=404, error="slot_not_found")
    serial = _serial_for_slot(slot_map, slot_id)
    if not serial:
        return FarmTaskResult(ok=False, http_status=404, error="slot_not_found")
    package = normalize_voidfix_package(
        request.payload.get("voidfix_package") or voidfix_package
    )
    if not package:
        return FarmTaskResult(
            ok=False,
            http_status=422,
            error="invalid_request",
            message="voidfix_package is not configured",
        )
    runner = command_runner or AdbCommandRunner(adb_path, timeout_seconds=20.0)
    try:
        state = runner.run(serial, ["get-state"]).stdout.strip()
    except AdbCommandError:
        state = ""
    if state != "device":
        return FarmTaskResult(ok=False, http_status=409, error="device_offline")
    warnings: list[str] = []
    info = apply_voidfix_keepalive(
        runner,
        serial,
        package,
        warnings,
        restart=True,
    )
    logger.info("voidfix_repair_ok slot=%s running=%s", slot_id, info["running"])
    return FarmTaskResult(
        ok=True,
        http_status=200,
        details={
            "ok": True,
            "slot_id": slot_id,
            "serial": serial,
            **info,
            "warnings": warnings,
        },
    )
