"""Fast ADB wake for live-stream first frame. Does not run screen prepare or lockdown.

``input keyevent 224`` is KEYCODE_WAKEUP. Sending it while the panel is already
on is a no-op. This task never dumpsys (S20 dumpsys is a common 30s+ stall).
"""
from __future__ import annotations

import logging
import subprocess
from typing import Any

from application.farm_task_types import FarmTaskRequest, FarmTaskResult
from infrastructure.adb_companion import AdbCommandError, AdbCommandRunner

logger = logging.getLogger("farm_agent.device_stream_wake")

DEVICE_STREAM_WAKE_TASK = "device_stream_wake"
WAKE_ADB_TIMEOUT_SECONDS = 3.0
KEYCODE_WAKEUP = "224"


def run_device_stream_wake(
    *,
    adb_path: str,
    slot_map: dict[int, str],
    request: FarmTaskRequest,
    command_runner: AdbCommandRunner | None = None,
) -> FarmTaskResult:
    serial = str(slot_map.get(int(request.farm_slot_id)) or "").strip()
    if not serial:
        return FarmTaskResult(ok=False, http_status=404, error="slot_not_found")
    args = ["shell", "input", "keyevent", KEYCODE_WAKEUP]
    try:
        if command_runner is not None:
            try:
                command_runner.run(serial, args, timeout_seconds=WAKE_ADB_TIMEOUT_SECONDS)
            except TypeError:
                command_runner.run(serial, args)
        else:
            completed = subprocess.run(
                [adb_path, "-s", serial, *args],
                capture_output=True,
                text=True,
                timeout=WAKE_ADB_TIMEOUT_SECONDS,
                check=False,
            )
            if completed.returncode != 0:
                raise AdbCommandError(completed.stderr.strip() or "wake_failed")
    except (AdbCommandError, OSError, subprocess.TimeoutExpired) as exc:
        logger.info("device_stream_wake_failed slot=%s err=%s", request.farm_slot_id, exc.__class__.__name__)
        return FarmTaskResult(ok=False, http_status=422, error="wake_failed")
    logger.info("device_stream_wake_ok slot=%s", request.farm_slot_id)
    return FarmTaskResult(ok=True, http_status=200, details={"woke": True, "keyevent": int(KEYCODE_WAKEUP)})


def request_device_stream_wake(farm_client: Any, slot_id: int) -> None:
    """Best-effort VPS → Farm KEYCODE_WAKEUP. Never raises; bounded timeout."""
    if farm_client is None:
        return
    try:
        farm_client.run_task(
            task_type=DEVICE_STREAM_WAKE_TASK,
            farm_slot_id=int(slot_id),
            payload={},
            job_id=f"stream-wake-{int(slot_id)}",
            timeout_seconds=WAKE_ADB_TIMEOUT_SECONDS + 2.0,
        )
    except Exception:
        logger.warning("device_stream_wake_request_failed slot=%s", slot_id)
