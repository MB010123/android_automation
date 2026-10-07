"""Customer phone remote-control surface (browser logic, tested here).

The live SlotRent page paints MJPEG by swapping ``<img src>`` on every JPEG
frame. That cancels pointer capture and often leaves naturalWidth at 0, so
clicks look like a still image. This module is the control surface: map
display coordinates onto the last decoded frame, classify tap vs swipe, and
emit only the VPS allowlist (tap / swipe / type / back).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

ALLOWED_ACTIONS = frozenset({"tap", "swipe", "type", "back"})
FORBIDDEN_ACTIONS = frozenset({"home", "recents", "notification", "keyevent", "adb", "shell"})
FORBIDDEN_KEYS = frozenset(
    {
        "slot_id",
        "farm_slot_id",
        "serial",
        "adb_serial",
        "udid",
        "device_id",
        "workspace_id",
        "command",
        "shell",
        "adb_command",
    }
)
# Display-space movement (CSS pixels) that turns a press into a swipe.
SWIPE_THRESHOLD_PX = 12.0
MAX_TEXT = 64


@dataclass(frozen=True)
class DisplayRect:
    left: float
    top: float
    width: float
    height: float


def map_display_to_device(
    client_x: float,
    client_y: float,
    rect: DisplayRect,
    device_width: int,
    device_height: int,
    *,
    object_fit: str = "contain",
) -> tuple[int, int] | None:
    """Map a pointer in viewport CSS pixels to native device pixels.

    ``object_fit=contain`` preserves aspect ratio (letterbox ignored).
    """
    if device_width <= 0 or device_height <= 0 or rect.width <= 0 or rect.height <= 0:
        return None
    dx = client_x - rect.left
    dy = client_y - rect.top
    if object_fit == "fill":
        x = dx / rect.width * device_width
        y = dy / rect.height * device_height
    else:
        scale = min(rect.width / device_width, rect.height / device_height)
        content_w = device_width * scale
        content_h = device_height * scale
        pad_x = (rect.width - content_w) / 2.0
        pad_y = (rect.height - content_h) / 2.0
        if dx < pad_x or dy < pad_y or dx > pad_x + content_w or dy > pad_y + content_h:
            return None
        x = (dx - pad_x) / scale
        y = (dy - pad_y) / scale
    xi = max(0, min(device_width - 1, int(round(x))))
    yi = max(0, min(device_height - 1, int(round(y))))
    return xi, yi


def classify_gesture(
    start_display: tuple[float, float],
    end_display: tuple[float, float],
    *,
    threshold_px: float = SWIPE_THRESHOLD_PX,
) -> str:
    dist = (
        (end_display[0] - start_display[0]) ** 2 + (end_display[1] - start_display[1]) ** 2
    ) ** 0.5
    return "swipe" if dist >= threshold_px else "tap"


def control_payload(action: str, **fields: Any) -> dict[str, Any] | None:
    act = str(action or "").strip().lower()
    if act in FORBIDDEN_ACTIONS or act not in ALLOWED_ACTIONS:
        return None
    if any(key in fields for key in FORBIDDEN_KEYS):
        return None
    if act == "back":
        return {"action": "back"}
    if act == "type":
        text = str(fields.get("text") or "")
        if not text or len(text) > MAX_TEXT:
            return None
        return {"action": "type", "text": text[:MAX_TEXT]}
    if act == "tap":
        return {"action": "tap", "x": int(fields["x"]), "y": int(fields["y"])}
    return {
        "action": "swipe",
        "x": int(fields["x"]),
        "y": int(fields["y"]),
        "x2": int(fields["x2"]),
        "y2": int(fields["y2"]),
    }


class PhoneRemoteController:
    """Pointer-event state machine for one in-app GADS session."""

    def __init__(self, send: Callable[[dict[str, Any]], None]) -> None:
        self._send = send
        self.session_connected = False
        self.stream_ready = False
        self.device_width = 0
        self.device_height = 0
        self.prevent_default = False
        self.prevent_scroll = False
        self.image_navigation_blocked = True
        self.indicator: tuple[float, float] | None = None
        self._press: dict[str, Any] | None = None

    @property
    def ready(self) -> bool:
        return bool(
            self.session_connected
            and self.stream_ready
            and self.device_width > 0
            and self.device_height > 0
        )

    def set_session_connected(self, connected: bool) -> None:
        self.session_connected = bool(connected)
        if not connected:
            self._press = None
            self.indicator = None

    def on_frame(self, width: int, height: int) -> None:
        if width > 0 and height > 0:
            self.device_width = int(width)
            self.device_height = int(height)
            self.stream_ready = True

    def pointer_down(self, client_x: float, client_y: float, rect: DisplayRect) -> bool:
        self.prevent_default = True
        self.prevent_scroll = True
        if not self.ready:
            self._press = None
            return False
        mapped = map_display_to_device(
            client_x, client_y, rect, self.device_width, self.device_height
        )
        if mapped is None:
            self._press = None
            return False
        self._press = {
            "display": (client_x, client_y),
            "device": mapped,
            "rect": rect,
        }
        self.indicator = (client_x - rect.left, client_y - rect.top)
        return True

    def pointer_move(self, client_x: float, client_y: float, rect: DisplayRect) -> None:
        if self._press is None:
            return
        self.prevent_default = True
        self.prevent_scroll = True
        self.indicator = (client_x - rect.left, client_y - rect.top)

    def pointer_up(self, client_x: float, client_y: float, rect: DisplayRect) -> dict[str, Any] | None:
        press = self._press
        self._press = None
        self.indicator = None
        if press is None or not self.ready:
            return None
        start_d = press["display"]
        kind = classify_gesture(start_d, (client_x, client_y))
        end = map_display_to_device(
            client_x, client_y, rect, self.device_width, self.device_height
        )
        start = press["device"]
        if kind == "tap":
            payload = control_payload("tap", x=start[0], y=start[1])
        else:
            if end is None:
                return None
            payload = control_payload("swipe", x=start[0], y=start[1], x2=end[0], y2=end[1])
        if payload is None:
            return None
        self._send(payload)
        return payload

    def pointer_cancel(self) -> None:
        self._press = None
        self.indicator = None

    def send_back(self) -> dict[str, Any] | None:
        if not self.ready:
            return None
        payload = control_payload("back")
        if payload:
            self._send(payload)
        return payload

    def send_type(self, text: str) -> dict[str, Any] | None:
        if not self.ready:
            return None
        payload = control_payload("type", text=text)
        if payload:
            self._send(payload)
        return payload
