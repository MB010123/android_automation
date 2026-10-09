"""Server-side allowlist for customer in-app phone control.

The browser may propose semantic actions only (tap/swipe/type/back/home/recents).
Raw keys, ADB, shell, device identity, and notification-shade pulls are rejected
here — never only in JS. Bottom-edge swipes are normal navigation, not forbidden.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

PUBLIC_CONTROL_ACTIONS = ("tap", "swipe", "type", "back", "home", "recents")
ALLOWED_ACTIONS = frozenset(PUBLIC_CONTROL_ACTIONS)
FORBIDDEN_ACTIONS = frozenset(
    {
        "notification",
        "notifications",
        "keyevent",
        "keycode",
        "adb",
        "app",
        "shell",
        "key",
        "power",
        "recent",
    }
)
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

_MAX_COORD = 8192
_MAX_TEXT = 64
_TOP_SHADE_Y = 80
_MIN_DURATION_MS = 1
_MAX_DURATION_MS = 5000

_TAP_KEYS = frozenset({"action", "x", "y"})
_TYPE_KEYS = frozenset({"action", "text"})
_NAV_KEYS = frozenset({"action"})
_SWIPE_KEYS = frozenset(
    {
        "action",
        "x",
        "y",
        "x2",
        "y2",
        "start_x",
        "start_y",
        "end_x",
        "end_y",
        "duration",
        "duration_ms",
    }
)


@dataclass(frozen=True)
class ControlCommand:
    action: str
    x: int | None = None
    y: int | None = None
    x2: int | None = None
    y2: int | None = None
    text: str | None = None
    duration_ms: int | None = None


@dataclass(frozen=True)
class ControlDecision:
    allowed: bool
    command: ControlCommand | None = None
    reason: str = ""


def decide_control(payload: Mapping[str, Any] | None, *, setup_mode: bool = False) -> ControlDecision:
    """Validate a customer control body. Fail closed on anything unknown.

    ``setup_mode`` keeps the public payload contract but rejects Home/Recents
    during eSIM setup. Normal/ready mode still allows those actions.
    """
    if not isinstance(payload, Mapping):
        return ControlDecision(False, reason="invalid_control")
    for key in payload:
        if str(key) in FORBIDDEN_PAYLOAD_KEYS:
            return ControlDecision(False, reason="forbidden_control")
    action = str(payload.get("action") or "").strip().lower()
    if not action:
        return ControlDecision(False, reason="invalid_control")
    if action in FORBIDDEN_ACTIONS:
        return ControlDecision(False, reason="forbidden_control")
    if action not in ALLOWED_ACTIONS:
        return ControlDecision(False, reason="invalid_control")
    if setup_mode and action in {"home", "recents"}:
        return ControlDecision(False, reason="forbidden_control")
    if action in {"back", "home", "recents"}:
        extra = {k for k in payload if k not in _NAV_KEYS}
        if extra:
            return ControlDecision(False, reason="invalid_control")
        return ControlDecision(True, ControlCommand(action=action))
    if action == "type":
        text = payload.get("text")
        if not isinstance(text, str) or not text or len(text) > _MAX_TEXT:
            return ControlDecision(False, reason="invalid_control")
        if any(ord(ch) < 32 for ch in text):
            return ControlDecision(False, reason="forbidden_control")
        extra = {k for k in payload if k not in _TYPE_KEYS}
        if extra:
            return ControlDecision(False, reason="invalid_control")
        return ControlDecision(True, ControlCommand(action="type", text=text))
    if action == "tap":
        extra = {k for k in payload if k not in _TAP_KEYS}
        if extra:
            return ControlDecision(False, reason="invalid_control")
        x = _coord(payload.get("x"))
        y = _coord(payload.get("y"))
        if x is None or y is None:
            return ControlDecision(False, reason="invalid_control")
        return ControlDecision(True, ControlCommand(action="tap", x=x, y=y))
    extra = {k for k in payload if k not in _SWIPE_KEYS}
    if extra:
        return ControlDecision(False, reason="invalid_control")
    x = _coord(payload.get("start_x") if payload.get("start_x") is not None else payload.get("x"))
    y = _coord(payload.get("start_y") if payload.get("start_y") is not None else payload.get("y"))
    x2 = _coord(payload.get("end_x") if payload.get("end_x") is not None else payload.get("x2"))
    y2 = _coord(payload.get("end_y") if payload.get("end_y") is not None else payload.get("y2"))
    if x is None or y is None or x2 is None or y2 is None:
        return ControlDecision(False, reason="invalid_control")
    duration_ms = _duration_ms(payload)
    if duration_ms is False:
        return ControlDecision(False, reason="invalid_control")
    if _is_notification_shade(y, y2):
        return ControlDecision(False, reason="forbidden_control")
    return ControlDecision(
        True,
        ControlCommand(action="swipe", x=x, y=y, x2=x2, y2=y2, duration_ms=duration_ms),
    )


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


def _duration_ms(payload: Mapping[str, Any]) -> int | None | bool:
    """Return duration, None if omitted, False if present but invalid."""
    if "duration_ms" in payload:
        raw = payload.get("duration_ms")
    elif "duration" in payload:
        raw = payload.get("duration")
    else:
        return None
    if isinstance(raw, bool) or raw is None:
        return False
    try:
        number = int(raw)
    except (TypeError, ValueError):
        return False
    if number < _MIN_DURATION_MS or number > _MAX_DURATION_MS:
        return False
    return number


def _is_notification_shade(y1: int, y2: int) -> bool:
    return y1 <= _TOP_SHADE_Y and (y2 - y1) >= 120
