"""Server-side allowlist for customer in-app phone control.

The browser may propose tap/swipe/type/back only. Home, Recents, notification
shade, raw keys, ADB, and device identity are rejected here — never only in JS.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

ALLOWED_ACTIONS = frozenset({"tap", "swipe", "type", "back"})
FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "slot_id",
        "farm_slot_id",
        "serial",
        "adb_serial",
        "device_serial",
        "udid",
        "device_id",
        "workspace_id",
        "workspace",
        "command",
        "shell",
        "adb_command",
        "keycode",
        "key",
        "home",
        "recents",
        "notification",
        "app",
        "package",
        "intent",
        "am",
    }
)

# Pixel-class portrait. Used only to reject edge gestures, not to accept coords.
_DEFAULT_HEIGHT = 2960
_MAX_COORD = 8192
_MAX_TEXT = 64
_TOP_SHADE_Y = 80
_BOTTOM_NAV_Y = 2500


@dataclass(frozen=True)
class ControlCommand:
    action: str
    x: int | None = None
    y: int | None = None
    x2: int | None = None
    y2: int | None = None
    text: str | None = None


@dataclass(frozen=True)
class ControlDecision:
    allowed: bool
    command: ControlCommand | None = None
    reason: str = ""


def decide_control(payload: Mapping[str, Any] | None) -> ControlDecision:
    """Validate a customer control body. Fail closed on anything unknown."""
    if not isinstance(payload, Mapping):
        return ControlDecision(False, reason="invalid_control")
    for key in payload:
        if str(key) in FORBIDDEN_PAYLOAD_KEYS:
            return ControlDecision(False, reason="forbidden_control")
    action = str(payload.get("action") or "").strip().lower()
    if action in {"home", "recents", "notification", "keyevent", "adb", "app"}:
        return ControlDecision(False, reason="forbidden_control")
    if action not in ALLOWED_ACTIONS:
        return ControlDecision(False, reason="forbidden_control")
    if action == "back":
        extra = {k for k in payload if k not in {"action"}}
        if extra:
            return ControlDecision(False, reason="invalid_control")
        return ControlDecision(True, ControlCommand(action="back"))
    if action == "type":
        text = payload.get("text")
        if not isinstance(text, str) or not text or len(text) > _MAX_TEXT:
            return ControlDecision(False, reason="invalid_control")
        if any(ord(ch) < 32 for ch in text):
            return ControlDecision(False, reason="forbidden_control")
        extra = {k for k in payload if k not in {"action", "text"}}
        if extra:
            return ControlDecision(False, reason="invalid_control")
        return ControlDecision(True, ControlCommand(action="type", text=text))
    x = _coord(payload.get("x"))
    y = _coord(payload.get("y"))
    if x is None or y is None:
        return ControlDecision(False, reason="invalid_control")
    if action == "tap":
        extra = {k for k in payload if k not in {"action", "x", "y"}}
        if extra:
            return ControlDecision(False, reason="invalid_control")
        return ControlDecision(True, ControlCommand(action="tap", x=x, y=y))
    x2 = _coord(payload.get("x2"))
    y2 = _coord(payload.get("y2"))
    if x2 is None or y2 is None:
        return ControlDecision(False, reason="invalid_control")
    extra = {k for k in payload if k not in {"action", "x", "y", "x2", "y2"}}
    if extra:
        return ControlDecision(False, reason="invalid_control")
    if _is_notification_shade(y, y2):
        return ControlDecision(False, reason="forbidden_control")
    if _is_home_or_recents_gesture(y, y2):
        return ControlDecision(False, reason="forbidden_control")
    return ControlDecision(True, ControlCommand(action="swipe", x=x, y=y, x2=x2, y2=y2))


def _coord(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < 0 or number > _MAX_COORD:
        return None
    return number


def _is_notification_shade(y1: int, y2: int) -> bool:
    return y1 <= _TOP_SHADE_Y and (y2 - y1) >= 120


def _is_home_or_recents_gesture(y1: int, y2: int) -> bool:
    """Upward swipe from the bottom gesture bar (Home / Recents)."""
    if y1 < _BOTTOM_NAV_Y:
        return False
    return y1 > y2 and (y1 - y2) >= 150 and y1 >= int(_DEFAULT_HEIGHT * 0.84)
