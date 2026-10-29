"""Read the on-device screen-state file and map it to ``screen_guidance`` JSON.

The 0.4s lockdown guard writes ``/data/local/tmp/mobirent_screen_state``
atomically. This module only ever runs ``adb shell cat`` on that one-line file:

* 0.4s per-serial cache so the dashboard bubble follows the phone in <1s;
* per-serial coalescer so concurrent polls for one slot share a single ADB
  call instead of flooding the USB hub next to GADS' live stream sockets;
* 1.5s ``cat`` timeout and *no* fallback to ``dumpsys``/``getprop`` — when the
  read is empty or slow we return the last known state for that serial.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
import uuid
from typing import Any

from application.device_hardware_lockdown import SCREEN_STATE_REMOTE
from application.farm_task_types import FarmTaskRequest, FarmTaskResult
from application.screen_guidance_catalog import (
    DEVICE_FAMILY_PIXEL,
    DEVICE_FAMILY_S20,
    SCREEN_KEY_HOME,
    SCREEN_KEYS,
    guidance_for,
    normalize_device_family,
)
from infrastructure.adb_companion import AdbCommandError, AdbCommandRunner

logger = logging.getLogger("farm_agent.screen_guidance")

CACHE_TTL_SECONDS = 0.4
CAT_TIMEOUT_SECONDS = 1.5
TASK_TIMEOUT_SECONDS = 2.5
DEVICE_SCREEN_GUIDANCE_TASK = "device_screen_guidance"

_adb_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_adb_lock = threading.Lock()
# One in-flight ADB read per serial; concurrent pollers wait on it and share it.
_serial_locks: dict[str, threading.Lock] = {}
_last_known_raw: dict[str, str] = {}
_vps_cache: dict[int, tuple[float, dict[str, Any]]] = {}
_vps_lock = threading.Lock()


def clear_screen_guidance_caches() -> None:
    with _adb_lock:
        _adb_cache.clear()
        _last_known_raw.clear()
    with _vps_lock:
        _vps_cache.clear()


def _serial_lock(serial: str) -> threading.Lock:
    with _adb_lock:
        lock = _serial_locks.get(serial)
        if lock is None:
            lock = threading.Lock()
            _serial_locks[serial] = lock
        return lock


def parse_screen_state_line(raw: str, *, now: float | None = None) -> tuple[str, str]:
    """Parse ``screen_key|reason|until_epoch`` from the on-device state file."""
    text = str(raw or "").replace("\r", "").strip()
    if not text:
        return SCREEN_KEY_HOME, ""
    line = text.splitlines()[0].strip()
    parts = line.split("|")
    key = str(parts[0] if parts else "").strip().lower()
    reason = str(parts[1] if len(parts) > 1 else "").strip().lower()
    until = 0
    if len(parts) > 2:
        try:
            until = int(str(parts[2]).strip() or "0")
        except ValueError:
            until = 0
    clock = time.time() if now is None else float(now)
    if key == "blocked_kick" and until > 0 and clock > until:
        return SCREEN_KEY_HOME, ""
    if key not in SCREEN_KEYS:
        return SCREEN_KEY_HOME, ""
    return key, reason


def device_family_for(
    *,
    slot_id: int,
    serial: str,
    manufacturer: str = "",
    model: str = "",
) -> str:
    from application.device_screen_prepare import is_samsung_s20
    from application.remote_access_farm_task import _is_samsung_renter_device

    if is_samsung_s20(
        slot_id=int(slot_id),
        serial=str(serial or ""),
        manufacturer=manufacturer,
        model=model,
    ):
        return DEVICE_FAMILY_S20
    if _is_samsung_renter_device(int(slot_id), str(serial or ""), manufacturer):
        return DEVICE_FAMILY_S20
    return normalize_device_family(manufacturer) if manufacturer else DEVICE_FAMILY_PIXEL


def public_screen_guidance(
    *,
    screen_key: str,
    device_family: str = DEVICE_FAMILY_PIXEL,
    blocked_reason: str = "",
) -> dict[str, Any]:
    return guidance_for(
        screen_key,
        device_family=device_family,
        blocked_reason=blocked_reason,
    )


def read_device_screen_guidance(
    runner: AdbCommandRunner | None,
    serial: str,
    *,
    slot_id: int,
    adb_path: str = "adb",
) -> dict[str, Any]:
    """Cat the on-device state file and return catalog JSON. Defaults to Home.

    Polling never runs dumpsys or getprop over ADB — only ``cat`` of the
    guard's one-line state file. An empty/slow read returns the last known
    state for this serial. OEM wording uses the mapped slot/serial.
    """
    cache_key = str(serial or "").strip()
    raw = _cat_screen_state(runner, serial, adb_path=adb_path)
    if cache_key:
        with _adb_lock:
            if raw.strip():
                _last_known_raw[cache_key] = raw
            else:
                raw = _last_known_raw.get(cache_key, "")
    key, reason = parse_screen_state_line(raw)
    family = device_family_for(slot_id=int(slot_id), serial=serial)
    return public_screen_guidance(
        screen_key=key,
        device_family=family,
        blocked_reason=reason,
    )


def cached_device_screen_guidance(
    runner: AdbCommandRunner | None,
    serial: str,
    *,
    slot_id: int,
    adb_path: str = "adb",
) -> dict[str, Any]:
    """Return guidance for this serial, at most one ADB ``cat`` per 0.4s.

    Concurrent callers for the same serial block on a per-serial lock and
    re-check the cache, so N simultaneous dashboard polls cost one ADB call.
    """
    cache_key = str(serial or "").strip()

    def _hit() -> dict[str, Any] | None:
        entry = _adb_cache.get(cache_key)
        if entry is not None and time.monotonic() - entry[0] < CACHE_TTL_SECONDS:
            return dict(entry[1])
        return None

    if not cache_key:
        return read_device_screen_guidance(
            runner, serial, slot_id=int(slot_id), adb_path=adb_path
        )
    with _adb_lock:
        cached = _hit()
    if cached is not None:
        return cached
    with _serial_lock(cache_key):
        with _adb_lock:
            cached = _hit()
        if cached is not None:
            return cached
        payload = read_device_screen_guidance(
            runner, serial, slot_id=int(slot_id), adb_path=adb_path
        )
        with _adb_lock:
            _adb_cache[cache_key] = (time.monotonic(), dict(payload))
    return payload


def run_device_screen_guidance(
    *,
    adb_path: str,
    slot_map: dict[int, str],
    request: FarmTaskRequest,
    command_runner: AdbCommandRunner | None = None,
) -> FarmTaskResult:
    """Farm task: fast read of the on-device screen-state file."""
    from domain.farm import MAX_FARM_SLOT_ID

    slot_id = int(request.farm_slot_id)
    if not 1 <= slot_id <= MAX_FARM_SLOT_ID:
        return FarmTaskResult(ok=False, http_status=404, error="slot_not_found")
    serial = str(slot_map.get(slot_id) or "").strip()
    if not serial:
        return FarmTaskResult(ok=False, http_status=404, error="slot_not_found")
    runner = command_runner or AdbCommandRunner(adb_path, timeout_seconds=CAT_TIMEOUT_SECONDS)
    guidance = cached_device_screen_guidance(
        runner, serial, slot_id=slot_id, adb_path=adb_path
    )
    return FarmTaskResult(
        ok=True,
        http_status=200,
        details={
            "ok": True,
            "slot_id": slot_id,
            "serial": serial,
            "screen_key": str(guidance.get("screen_key") or SCREEN_KEY_HOME),
            "screen_guidance": guidance,
        },
    )


def fetch_screen_guidance_via_farm(farm_client: Any, slot_id: int) -> dict[str, Any] | None:
    """VPS-side 0.4s cache around Farm ``device_screen_guidance``."""
    bay = int(slot_id)

    def _hit() -> dict[str, Any] | None:
        entry = _vps_cache.get(bay)
        if entry is not None and time.monotonic() - entry[0] < CACHE_TTL_SECONDS:
            return dict(entry[1])
        return None

    with _vps_lock:
        cached = _hit()
    if cached is not None:
        return cached
    if farm_client is None:
        return None
    # Coalesce concurrent dashboard polls (device-status + remote-access +
    # screen-guidance) for one bay into a single Farm round-trip.
    with _serial_lock(f"bay:{bay}"):
        with _vps_lock:
            cached = _hit()
        if cached is not None:
            return cached
        return _fetch_screen_guidance_uncached(farm_client, bay)


def _fetch_screen_guidance_uncached(farm_client: Any, bay: int) -> dict[str, Any] | None:
    kwargs: dict[str, Any] = {
        "task_type": DEVICE_SCREEN_GUIDANCE_TASK,
        "farm_slot_id": bay,
        "payload": {},
        "job_id": str(uuid.uuid4()),
    }
    try:
        try:
            response = farm_client.run_task(**kwargs, timeout_seconds=TASK_TIMEOUT_SECONDS)
        except TypeError:
            response = farm_client.run_task(**kwargs)
    except Exception:
        logger.warning("screen_guidance_farm_failed slot=%s", bay)
        return None
    if not getattr(response, "ok", False):
        return None
    raw_body = getattr(response, "body", None)
    details: dict[str, Any] = {}
    if isinstance(raw_body, dict):
        inner = raw_body.get("details")
        details = dict(inner) if isinstance(inner, dict) else dict(raw_body)
    guidance = details.get("screen_guidance")
    if not isinstance(guidance, dict):
        return None
    with _vps_lock:
        _vps_cache[bay] = (time.monotonic(), dict(guidance))
    return dict(guidance)


def attach_screen_guidance(body: dict[str, Any], guidance: dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(guidance, dict) and guidance.get("screen_key"):
        body["screen_guidance"] = dict(guidance)
    return body


ADB_SERVER_ADDR = ("127.0.0.1", 5037)


def _adb_server_shell(serial: str, command: str, *, timeout: float) -> str | None:
    """Run one shell command through the adb *server* socket (no adb.exe spawn).

    Spawning ``adb.exe`` on Windows costs ~40-60ms per call; the server socket
    round-trip is ~20-30ms. Returns ``None`` on any protocol/socket problem so
    the caller can fall back to the subprocess path.
    """
    import socket

    def _send(sock: socket.socket, payload: str) -> None:
        data = payload.encode("utf-8")
        sock.sendall(f"{len(data):04x}".encode("ascii") + data)

    def _okay(sock: socket.socket) -> bool:
        return sock.recv(4) == b"OKAY"

    try:
        with socket.create_connection(ADB_SERVER_ADDR, timeout=timeout) as sock:
            sock.settimeout(timeout)
            _send(sock, f"host:transport:{serial}")
            if not _okay(sock):
                return None
            _send(sock, f"shell:{command}")
            if not _okay(sock):
                return None
            chunks: list[bytes] = []
            while True:
                piece = sock.recv(4096)
                if not piece:
                    break
                chunks.append(piece)
            return b"".join(chunks).decode("utf-8", "replace")
    except (OSError, ValueError):
        return None


def _cat_screen_state(
    runner: AdbCommandRunner | None,
    serial: str,
    *,
    adb_path: str,
) -> str:
    try:
        # Fast path for the real runner: adb-server socket, no process spawn.
        # Test doubles (subclasses) keep going through ``runner.run``.
        if runner is None or type(runner) is AdbCommandRunner:
            fast = _adb_server_shell(
                serial, f"cat {SCREEN_STATE_REMOTE}", timeout=CAT_TIMEOUT_SECONDS
            )
            if fast is not None:
                return fast
        if runner is not None:
            return runner.run(serial, ["shell", "cat", SCREEN_STATE_REMOTE]).stdout or ""
        completed = subprocess.run(
            [adb_path, "-s", serial, "shell", "cat", SCREEN_STATE_REMOTE],
            capture_output=True,
            text=True,
            timeout=CAT_TIMEOUT_SECONDS,
            check=False,
        )
        return completed.stdout or ""
    except (AdbCommandError, OSError, subprocess.TimeoutExpired):
        return ""


