"""GADS MJPEG quality / FPS / scale. Applied via Hub APIs, not ADB.

Override with ``MOBI_GADS_JPEG_QUALITY``, ``MOBI_GADS_TARGET_FPS``,
``MOBI_GADS_SCALING_FACTOR``. Unset keeps these defaults (60 / 12 / 50).
"""
from __future__ import annotations

import os
from typing import Any


def _env_int(name: str, default: int) -> int:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value


JPEG_QUALITY = _env_int("MOBI_GADS_JPEG_QUALITY", 60)
TARGET_FPS = _env_int("MOBI_GADS_TARGET_FPS", 12)
SCALING_FACTOR = _env_int("MOBI_GADS_SCALING_FACTOR", 50)


def device_stream_settings_payload() -> dict[str, int]:
    return {
        "jpeg_quality": int(JPEG_QUALITY),
        "target_fps": int(TARGET_FPS),
        "scaling_factor": int(SCALING_FACTOR),
    }


def global_stream_settings_payload() -> dict[str, int]:
    scale = int(SCALING_FACTOR)
    return {
        "jpeg_quality": int(JPEG_QUALITY),
        "target_fps": int(TARGET_FPS),
        "scaling_factor_android": scale,
        "scaling_factor_ios": scale,
    }


def matches_live_info(info: dict[str, Any] | None) -> bool:
    raw = info if isinstance(info, dict) else {}
    try:
        jpeg = int(raw.get("stream_jpeg_quality"))
        fps = int(raw.get("stream_target_fps"))
        scale = int(raw.get("stream_scaling_factor"))
    except (TypeError, ValueError):
        return False
    return jpeg == JPEG_QUALITY and fps == TARGET_FPS and scale == SCALING_FACTOR
