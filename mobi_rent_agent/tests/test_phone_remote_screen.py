"""Customer phone screen: pointer overlay, device-pixel mapping, allowlist."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from web.phone_remote_screen import (
    ALLOWED_ACTIONS,
    FORBIDDEN_ACTIONS,
    DisplayRect,
    PhoneRemoteController,
    classify_gesture,
    control_payload,
    map_display_to_device,
)

DEVICE = (1080, 2400)
FULL = DisplayRect(left=0, top=0, width=360, height=800)


def _ready(send=None) -> tuple[PhoneRemoteController, list]:
    sent: list = []
    ctl = PhoneRemoteController(send or sent.append)
    ctl.set_session_connected(True)
    ctl.on_frame(*DEVICE)
    return ctl, sent


def test_center_tap_sends_device_coordinates():
    ctl, sent = _ready()
    rect = FULL
    ctl.pointer_down(180, 400, rect)
    payload = ctl.pointer_up(180, 400, rect)
    assert payload == {"action": "tap", "x": 540, "y": 1200}
    assert sent == [payload]


def test_edge_taps_map_to_device_edges():
    ctl, sent = _ready()
    # contain: 360x800 vs 1080x2400 → scale=1/3, content fills the rect exactly.
    cases = [
        (0, 0, 0, 0),
        (359, 0, 1077, 0),
        (0, 799, 0, 2397),
        (359, 799, 1077, 2397),
    ]
    for cx, cy, dx, dy in cases:
        ctl.pointer_down(cx, cy, FULL)
        payload = ctl.pointer_up(cx, cy, FULL)
        assert payload["action"] == "tap"
        assert payload["x"] == dx and payload["y"] == dy, (cx, cy, payload)
    assert len(sent) == 4


def test_responsive_resize_preserves_mapping():
    small = DisplayRect(0, 0, 180, 400)
    large = DisplayRect(0, 0, 360, 800)
    a = map_display_to_device(90, 200, small, *DEVICE)
    b = map_display_to_device(180, 400, large, *DEVICE)
    assert a == b == (540, 1200)
    letterboxed = DisplayRect(0, 0, 800, 800)
    mapped = map_display_to_device(400, 400, letterboxed, *DEVICE)
    assert mapped == (540, 1200)
    # Click in the side letterbox is ignored (not stretched).
    assert map_display_to_device(10, 400, letterboxed, *DEVICE) is None


def test_mouse_drag_becomes_swipe():
    ctl, sent = _ready()
    ctl.pointer_down(180, 600, FULL)
    ctl.pointer_move(180, 200, FULL)
    payload = ctl.pointer_up(180, 200, FULL)
    assert payload["action"] == "swipe"
    assert payload["x"] == 540 and payload["y"] == 1800
    assert payload["x2"] == 540 and payload["y2"] == 600
    assert sent == [payload]


def test_touch_drag_uses_same_pointer_path():
    ctl, sent = _ready()
    # Same Pointer Event path for mouse and touch.
    ctl.pointer_down(40, 700, FULL)
    ctl.pointer_up(300, 100, FULL)
    assert sent[0]["action"] == "swipe"
    assert sent[0]["x"] < sent[0]["x2"]
    assert sent[0]["y"] > sent[0]["y2"]


def test_tap_does_not_become_swipe():
    assert classify_gesture((10, 10), (10, 10)) == "tap"
    assert classify_gesture((10, 10), (18, 16)) == "tap"
    assert classify_gesture((10, 10), (10, 30)) == "swipe"
    ctl, sent = _ready()
    ctl.pointer_down(180, 400, FULL)
    ctl.pointer_up(186, 404, FULL)
    assert sent[0]["action"] == "tap"


def test_image_cannot_open_or_navigate():
    ctl, _ = _ready()
    captured = ctl.pointer_down(180, 400, FULL)
    assert captured is True
    assert ctl.prevent_default is True
    assert ctl.image_navigation_blocked is True
    js = (ROOT / "web" / "phone_remote_screen.js").read_text(encoding="utf-8")
    tsx = (ROOT / "web" / "PhoneRemoteScreen.tsx").read_text(encoding="utf-8")
    css = (ROOT / "web" / "phone_remote_screen.css").read_text(encoding="utf-8")
    for source in (js, tsx):
        assert "phone-remote-hit" in source
        assert "setPointerCapture" in source
        assert "querySelector(\"img\")" not in source
        assert "img.src" not in source
    assert "<canvas" in tsx
    assert "pointer-events: none" in css


def test_touch_does_not_scroll_parent():
    ctl, _ = _ready()
    ctl.pointer_down(180, 400, FULL)
    ctl.pointer_move(180, 380, FULL)
    assert ctl.prevent_scroll is True
    assert ctl.prevent_default is True
    css = (ROOT / "web" / "phone_remote_screen.css").read_text(encoding="utf-8")
    assert "touch-action: none" in css


def test_back_and_type_work_when_ready():
    ctl, sent = _ready()
    assert ctl.send_back() == {"action": "back"}
    assert ctl.send_type("hello") == {"action": "type", "text": "hello"}
    assert sent[-2:] == [{"action": "back"}, {"action": "type", "text": "hello"}]


def test_home_recents_shade_remain_unavailable():
    ctl, sent = _ready()
    assert control_payload("home") is None
    assert control_payload("recents") is None
    assert control_payload("notification") is None
    assert control_payload("tap", x=1, y=1, serial="X") is None
    assert control_payload("tap", x=1, y=1, udid="X") is None
    assert control_payload("tap", x=1, y=1, workspace_id="ws") is None
    assert FORBIDDEN_ACTIONS.isdisjoint(ALLOWED_ACTIONS)
    js = (ROOT / "web" / "phone_remote_screen.js").read_text(encoding="utf-8")
    assert "home: 1" in js and "recents: 1" in js
    assert sent == []


def test_controls_disabled_before_session_and_stream_ready():
    sent: list = []
    ctl = PhoneRemoteController(sent.append)
    rect = FULL
    assert ctl.ready is False
    assert ctl.pointer_down(180, 400, rect) is False
    assert ctl.pointer_up(180, 400, rect) is None
    assert ctl.send_back() is None
    assert ctl.send_type("x") is None
    ctl.set_session_connected(True)
    assert ctl.ready is False
    ctl.on_frame(1080, 2400)
    assert ctl.ready is True
    ctl.pointer_down(180, 400, rect)
    ctl.pointer_up(180, 400, rect)
    assert sent and sent[0]["action"] == "tap"


def test_auth_and_rental_id_stay_out_of_control_payloads():
    ctl, sent = _ready()
    ctl.pointer_down(100, 100, FULL)
    ctl.pointer_up(100, 100, FULL)
    body = sent[0]
    for banned in (
        "serial",
        "udid",
        "workspace_id",
        "slot_id",
        "device_id",
        "platform_login",
        "rental_id",
    ):
        assert banned not in body
    assert set(body) <= {"action", "x", "y", "x2", "y2", "text"}
