"""Read-only native display size from Android ``wm size``.

Authoritative source is the phone's Physical size. This is not the GADS
MJPEG frame size. Never invents a Pixel-class default.
"""
from __future__ import annotations

import logging
import re
import subprocess
from typing import Any

from application.farm_task_types import FarmTaskRequest, FarmTaskResult
from infrastructure.adb_companion import AdbCommandError, AdbCommandRunner

logger = logging.getLogger("farm_agent.device_display_size")

DEVICE_DISPLAY_SIZE_TASK = "device_display_size"

_PHYSICAL_RE = re.compile(r"Physical size:\s*(\d+)\s*x\s*(\d+)", re.IGNORECASE)
_OVERRIDE_RE = re.compile(r"Override size:\s*(\d+)\s*x\s*(\d+)", re.IGNORECASE)


def parse_wm_size(text: str | None) -> tuple[int, int] | None:
    """Return physical WxH from ``adb shell wm size`` output, or None."""
    blob = str(text or "")
    match = _PHYSICAL_RE.search(blob)
    if match is None:
        match = _OVERRIDE_RE.search(blob)
    if match is None:
        return None
    width = int(match.group(1))
    height = int(match.group(2))
    if width <= 0 or height <= 0:
        return None
    return width, height


def native_resolution_body(width: int, height: int) -> dict[str, int]:
    return {"width": int(width), "height": int(height)}


def run_device_display_size(
    *,
    adb_path: str,
    slot_map: dict[int, str],
    request: FarmTaskRequest,
    command_runner: AdbCommandRunner | None = None,
) -> FarmTaskResult:
    """Read-only ``wm size``. No GADS, no eSIM, no input, no settings change."""
    serial = str(slot_map.get(int(request.farm_slot_id)) or "").strip()
    if not serial:
        return FarmTaskResult(ok=False, http_status=404, error="slot_not_found")
    try:
        if command_runner is not None:
            output = command_runner.run(serial, ["shell", "wm", "size"]).stdout or ""
        else:
            completed = subprocess.run(
                [adb_path, "-s", serial, "shell", "wm", "size"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            output = completed.stdout or ""
    except (AdbCommandError, OSError, subprocess.TimeoutExpired):
        logger.warning("device_display_size_adb_failed slot=%s", request.farm_slot_id)
        return FarmTaskResult(ok=False, http_status=422, error="wm_size_unavailable")
    parsed = parse_wm_size(output)
    if parsed is None:
        return FarmTaskResult(ok=False, http_status=422, error="wm_size_unreported")
    width, height = parsed
    details: dict[str, Any] = {"width": width, "height": height, "source": "wm_size_physical"}
    return FarmTaskResult(ok=True, http_status=200, details=details)
