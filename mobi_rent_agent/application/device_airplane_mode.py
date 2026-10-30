"""Toggle Airplane Mode on unrooted Pixels and Samsung One UI via ADB.

Sequence:

1. ``cmd connectivity airplane-mode enable|disable`` — this is the command
   that actually flips the radio stack on modern Android.
2. Immediately after enable, keep Wi-Fi up (``cmd wifi set-wifi-enabled
   enabled`` and ``svc wifi enable``) so GADS / remote control does not drop.
3. ``settings put global airplane_mode_on 0|1`` plus the AIRPLANE_MODE
   broadcast as fallback (broadcast is usually denied for shell uid 2000).
4. Read back BOTH ``cmd connectivity airplane-mode`` (enabled/disabled)
   AND ``settings get global airplane_mode_on`` (1/0). Report success only
   when both agree with the requested state.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Callable

from application.farm_task_types import FarmTaskRequest, FarmTaskResult
from domain.farm import MAX_FARM_SLOT_ID
from infrastructure.adb_companion import AdbCommandError, AdbCommandRunner

logger = logging.getLogger("farm_agent.airplane_mode")

AIRPLANE_MODE_TASK = "airplane_mode"
AIRPLANE_CYCLE_TASK = "airplane_cycle"
AIRPLANE_TIMEOUT_SECONDS = 20.0
_RENTAL_DISABLED_PATH = Path(__file__).resolve().parents[1] / "logs" / "rental_disabled_slots.json"
_RENTAL_DISABLED_LOCK = threading.Lock()
AIRPLANE_SETTING_GET = ["shell", "settings", "get", "global", "airplane_mode_on"]
AIRPLANE_CMD_GET = ["shell", "cmd", "connectivity", "airplane-mode"]
WIFI_ENABLE_COMMANDS = (
    ["shell", "cmd", "wifi", "set-wifi-enabled", "enabled"],
    ["shell", "svc", "wifi", "enable"],
)


def _serial_for_slot(slot_map: dict[int, str], farm_slot_id: int) -> str | None:
    serial = str(slot_map.get(int(farm_slot_id)) or "").strip()
    return serial or None


def normalize_airplane_state(value: Any) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    text = str(value or "").strip().lower()
    if text in {"1", "on", "enable", "enabled", "true", "yes"}:
        return "on"
    if text in {"0", "off", "disable", "disabled", "false", "no"}:
        return "off"
    return "toggle"


def requested_airplane_state(payload: dict[str, Any] | None) -> str:
    """Accept `state=on|off` or `enabled=true|false` from admin/farm callers."""
    body = payload if isinstance(payload, dict) else {}
    if "enabled" in body and body.get("enabled") is not None:
        enabled = body.get("enabled")
        if isinstance(enabled, bool):
            return "on" if enabled else "off"
        return normalize_airplane_state(enabled)
    return normalize_airplane_state(body.get("state"))


def airplane_is_on(state: str | None) -> bool:
    return str(state or "").strip().lower() in {"on", "enabled", "enable", "true", "1"}


def public_airplane_body(
    *,
    slot_id: int,
    state: str | None,
    serial: str | None = None,
) -> dict[str, Any]:
    enabled = airplane_is_on(state)
    body: dict[str, Any] = {
        "ok": True,
        "enabled": enabled,
        "state": "on" if enabled else "off",
        "slot_id": int(slot_id),
    }
    if serial:
        body["serial"] = serial
    return body


def persist_farm_rental_disabled(slot_id: int, disabled: bool, path: Path | None = None) -> None:
    target = path or _RENTAL_DISABLED_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with _RENTAL_DISABLED_LOCK:
            slots: list[int] = []
            if target.is_file():
                try:
                    raw = json.loads(target.read_text(encoding="utf-8"))
                    slots = [int(s) for s in (raw.get("slots") if isinstance(raw, dict) else raw) or []]
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    slots = []
            bay = int(slot_id)
            if disabled:
                if bay not in slots:
                    slots.append(bay)
            else:
                slots = [s for s in slots if s != bay]
            target.write_text(json.dumps({"slots": slots}), encoding="utf-8")
    except OSError:
        logger.warning("rental_disabled_persist_failed slot=%s", slot_id)


def farm_slot_rental_disabled(slot_id: int, path: Path | None = None) -> bool:
    target = path or _RENTAL_DISABLED_PATH
    try:
        if not target.is_file():
            return False
        raw = json.loads(target.read_text(encoding="utf-8"))
        slots = raw.get("slots") if isinstance(raw, dict) else raw
        return int(slot_id) in {int(s) for s in slots or []}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def parse_airplane_state(stdout: str) -> str | None:
    text = (stdout or "").strip().lower()
    if text in {"1", "true", "on"}:
        return "on"
    if text in {"0", "false", "off", "null"}:
        return "off"
    if "disabled" in text:
        return "off"
    if "enabled" in text:
        return "on"
    if text.endswith("disable"):
        return "off"
    if text.endswith("enable"):
        return "on"
    return None


def _short_adb_error(exc: AdbCommandError) -> str:
    return str(exc).replace("\n", " ").strip()[:180]


def _run_optional(
    runner: AdbCommandRunner,
    serial: str,
    arguments: list[str],
    warnings: list[str],
    label: str,
) -> None:
    try:
        runner.run(serial, list(arguments))
    except AdbCommandError as exc:
        warnings.append(f"{label}: {_short_adb_error(exc)}")
        logger.info("airplane_mode_%s_failed serial_suffix=%s", label.replace(" ", "_"), serial[-4:])


def _ensure_wifi_enabled(runner: AdbCommandRunner, serial: str, warnings: list[str]) -> None:
    """Airplane ON must not drop the Wi-Fi path GADS uses for remote control."""
    for arguments in WIFI_ENABLE_COMMANDS:
        _run_optional(runner, serial, arguments, warnings, "wifi enable")


def _read_settings_state(runner: AdbCommandRunner, serial: str) -> str | None:
    try:
        out = runner.run(serial, list(AIRPLANE_SETTING_GET)).stdout
        return parse_airplane_state(out)
    except AdbCommandError:
        logger.info("airplane_mode_settings_get_failed serial_suffix=%s", serial[-4:])
        return None


def _read_cmd_state(runner: AdbCommandRunner, serial: str) -> str | None:
    try:
        out = runner.run(serial, list(AIRPLANE_CMD_GET)).stdout
        return parse_airplane_state(out)
    except AdbCommandError:
        logger.info("airplane_mode_cmd_get_failed serial_suffix=%s", serial[-4:])
        return None


def _read_state(runner: AdbCommandRunner, serial: str) -> str | None:
    """Settings-only read used for the pre-toggle snapshot."""
    return _read_settings_state(runner, serial)


def _read_verified_state(runner: AdbCommandRunner, serial: str) -> str | None:
    """Both radios of truth must agree before the API reports on/off."""
    cmd_state = _read_cmd_state(runner, serial)
    settings_state = _read_settings_state(runner, serial)
    if cmd_state is None or settings_state is None:
        return None
    if cmd_state != settings_state:
        logger.info(
            "airplane_mode_readback_mismatch cmd=%s settings=%s serial_suffix=%s",
            cmd_state,
            settings_state,
            serial[-4:],
        )
        return None
    return cmd_state


def _apply_settings_and_broadcast(
    runner: AdbCommandRunner,
    serial: str,
    desired: str,
    warnings: list[str],
) -> None:
    flag = "1" if desired == "on" else "0"
    ez_state = "true" if desired == "on" else "false"
    verb = "enable" if desired == "on" else "disable"
    # Privileged connectivity command is what actually actuates radios.
    _run_optional(
        runner,
        serial,
        ["shell", "cmd", "connectivity", "airplane-mode", verb],
        warnings,
        "cmd connectivity airplane-mode",
    )
    if desired == "on":
        _ensure_wifi_enabled(runner, serial, warnings)
    _run_optional(
        runner,
        serial,
        ["shell", "settings", "put", "global", "airplane_mode_on", flag],
        warnings,
        "settings put",
    )
    _run_optional(
        runner,
        serial,
        [
            "shell",
            "am",
            "broadcast",
            "-a",
            "android.intent.action.AIRPLANE_MODE",
            "--ez",
            "state",
            ez_state,
        ],
        warnings,
        "AIRPLANE_MODE broadcast",
    )
    if desired == "on":
        _ensure_wifi_enabled(runner, serial, warnings)


def apply_airplane_mode(
    runner: AdbCommandRunner,
    serial: str,
    desired: str,
    warnings: list[str],
) -> str | None:
    _apply_settings_and_broadcast(runner, serial, desired, warnings)
    verified = _read_verified_state(runner, serial)
    if verified == desired:
        return verified
    verb = "enable" if desired == "on" else "disable"
    _run_optional(
        runner,
        serial,
        ["shell", "cmd", "connectivity", "airplane-mode", verb],
        warnings,
        "cmd connectivity airplane-mode retry",
    )
    if desired == "on":
        _ensure_wifi_enabled(runner, serial, warnings)
        _run_optional(
            runner,
            serial,
            ["shell", "settings", "put", "global", "airplane_mode_on", "1"],
            warnings,
            "settings put retry",
        )
    else:
        _run_optional(
            runner,
            serial,
            ["shell", "settings", "put", "global", "airplane_mode_on", "0"],
            warnings,
            "settings put retry",
        )
    return _read_verified_state(runner, serial)


def run_airplane_mode(
    *,
    adb_path: str,
    slot_map: dict[int, str],
    request: FarmTaskRequest,
    command_runner: AdbCommandRunner | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> FarmTaskResult:
    _ = sleep_fn
    slot_id = int(request.farm_slot_id)
    if not 1 <= slot_id <= MAX_FARM_SLOT_ID:
        return FarmTaskResult(ok=False, http_status=404, error="slot_not_found")
    serial = _serial_for_slot(slot_map, slot_id)
    if not serial:
        return FarmTaskResult(ok=False, http_status=404, error="slot_not_found")
    runner = command_runner or AdbCommandRunner(adb_path, timeout_seconds=AIRPLANE_TIMEOUT_SECONDS)
    try:
        state = runner.run(serial, ["get-state"]).stdout.strip()
    except AdbCommandError:
        state = ""
    if state != "device":
        return FarmTaskResult(ok=False, http_status=409, error="device_offline")

    warnings: list[str] = []
    payload = request.payload if isinstance(request.payload, dict) else {}
    requested = requested_airplane_state(payload)
    current = _read_state(runner, serial)
    if requested == "toggle":
        desired = "off" if current == "on" else "on"
    else:
        desired = requested
    try:
        applied = apply_airplane_mode(runner, serial, desired, warnings)
    except AdbCommandError as exc:
        logger.info("airplane_mode_failed slot=%s", slot_id)
        return FarmTaskResult(
            ok=False,
            http_status=502,
            error="airplane_mode_failed",
            details={"serial": serial, "reason": str(exc)[:240], "warnings": warnings},
        )
    if "rental_disabled" in payload:
        persist_farm_rental_disabled(slot_id, bool(payload.get("rental_disabled")))
    if applied != desired:
        logger.info("airplane_mode_unverified slot=%s desired=%s applied=%s", slot_id, desired, applied)
        return FarmTaskResult(
            ok=False,
            http_status=502,
            error="airplane_mode_failed",
            details={
                "serial": serial,
                "requested": requested,
                "desired": desired,
                "applied": applied,
                "method": "cmd_connectivity",
                "warnings": warnings,
            },
        )
    logger.info("airplane_mode_ok slot=%s desired=%s applied=%s", slot_id, desired, applied)
    details = {
        **public_airplane_body(slot_id=slot_id, state=applied, serial=serial),
        "requested": requested,
        "desired": desired,
        "method": "cmd_connectivity",
        "setting": "settings get global airplane_mode_on",
        "connectivity": "cmd connectivity airplane-mode",
        "warnings": warnings,
    }
    if desired == "on":
        details["wifi_preserved"] = True
    return FarmTaskResult(ok=True, http_status=200, details=details)


def run_airplane_cycle(
    *,
    adb_path: str,
    slot_map: dict[int, str],
    request: FarmTaskRequest,
    command_runner: AdbCommandRunner | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> FarmTaskResult:
    sleeper = sleep_fn or __import__("time").sleep
    try:
        delay = float(request.payload.get("on_seconds") or 2.0)
    except (TypeError, ValueError):
        delay = 2.0
    delay = max(0.0, min(delay, 15.0))
    on_request = FarmTaskRequest(
        job_id=request.job_id,
        task_type=AIRPLANE_MODE_TASK,
        farm_slot_id=request.farm_slot_id,
        payload={"state": "on"},
    )
    first = run_airplane_mode(
        adb_path=adb_path,
        slot_map=slot_map,
        request=on_request,
        command_runner=command_runner,
    )
    if not first.ok:
        return first
    if delay:
        sleeper(delay)
    off_request = FarmTaskRequest(
        job_id=request.job_id,
        task_type=AIRPLANE_MODE_TASK,
        farm_slot_id=request.farm_slot_id,
        payload={"state": "off"},
    )
    second = run_airplane_mode(
        adb_path=adb_path,
        slot_map=slot_map,
        request=off_request,
        command_runner=command_runner,
    )
    details = dict(second.details or {})
    details["cycled"] = True
    details["on_state"] = (first.details or {}).get("state")
    return FarmTaskResult(
        ok=second.ok,
        http_status=second.http_status,
        error=second.error,
        details=details,
    )
