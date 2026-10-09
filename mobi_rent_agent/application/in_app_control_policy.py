"""Server-side allowlist for customer in-app phone control.

The browser may propose semantic actions only (tap/swipe/type/back/home/recents/
notification_shade/quick_settings/rotate). Raw keys, ADB, shell, and device
identity are rejected here — never only in JS.

Assigned-customer control is always normal Android access: Home, Recents,
notification shade, Quick Settings, rotate, Settings, apps, and gestures are
allowed before, during, and after eSIM activation. ``setup_mode`` is telemetry
only and does not gate this allowlist. Bottom-edge swipes and status-bar
pull-downs are forwarded as normal navigation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

SETUP_ALLOWED_ACTIONS = ("tap", "swipe", "type", "back")
READY_EXTRA_ACTIONS = (
    "home",
    "recents",
    "notification_shade",
    "quick_settings",
    "rotate",
)
PUBLIC_CONTROL_ACTIONS = SETUP_ALLOWED_ACTIONS + READY_EXTRA_ACTIONS
ALLOWED_ACTIONS = frozenset(PUBLIC_CONTROL_ACTIONS)
SETUP_DENIED_ACTIONS: tuple[str, ...] = ()
NAV_ACTIONS = frozenset({"back", "home", "recents", "notification_shade", "quick_settings"})
_ACTION_ALIASES = {
    "overview": "recents",
    "recent": "recents",
    "notifications": "notification_shade",
    "notification": "notification_shade",
    "qs": "quick_settings",
    "quicksettings": "quick_settings",
}
FORBIDDEN_ACTIONS = frozenset(
    {
        "keyevent",
        "keycode",
        "adb",
        "app",
        "shell",
        "key",
        "power",
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
_ALLOWED_ORIENTATIONS = frozenset({"portrait", "landscape"})

_TAP_KEYS = frozenset({"action", "x", "y"})
_TYPE_KEYS = frozenset({"action", "text"})
_NAV_KEYS = frozenset({"action"})
_ROTATE_KEYS = frozenset({"action", "orientation"})
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
    orientation: str | None = None


@dataclass(frozen=True)
class ControlDecision:
    allowed: bool
    command: ControlCommand | None = None
    reason: str = ""


def public_allowed_controls(*, setup_mode: bool = False) -> list[str]:
    """Full normal Android actions from session start. ``setup_mode`` is unused."""
    _ = setup_mode
    return list(PUBLIC_CONTROL_ACTIONS)


def decide_control(payload: Mapping[str, Any] | None, *, setup_mode: bool = False) -> ControlDecision:
    """Validate a customer control body. Fail closed on anything unknown.

    ``setup_mode`` is accepted for call-site compatibility and does not restrict
    Home, Recents, shade, Quick Settings, rotate, or shade swipes.
    """
    _ = setup_mode
    if not isinstance(payload, Mapping):
        return ControlDecision(False, reason="invalid_control")
    for key in payload:
        if str(key) in FORBIDDEN_PAYLOAD_KEYS:
            return ControlDecision(False, reason="forbidden_control")
    action = str(payload.get("action") or "").strip().lower()
    if not action:
        return ControlDecision(False, reason="invalid_control")
    action = _ACTION_ALIASES.get(action, action)
    if action in FORBIDDEN_ACTIONS:
        return ControlDecision(False, reason="forbidden_control")
    if action not in ALLOWED_ACTIONS:
        return ControlDecision(False, reason="invalid_control")
    if action in NAV_ACTIONS:
        extra = {k for k in payload if k not in _NAV_KEYS}
        if extra:
            return ControlDecision(False, reason="invalid_control")
        return ControlDecision(True, ControlCommand(action=action))
    if action == "rotate":
        extra = {k for k in payload if k not in _ROTATE_KEYS}
        if extra:
            return ControlDecision(False, reason="invalid_control")
        orientation = str(payload.get("orientation") or "").strip().lower()
        if orientation not in _ALLOWED_ORIENTATIONS:
            return ControlDecision(False, reason="invalid_control")
        return ControlDecision(True, ControlCommand(action="rotate", orientation=orientation))
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
