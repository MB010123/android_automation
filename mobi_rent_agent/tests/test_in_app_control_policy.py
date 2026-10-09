"""Server-side in-app control allowlist."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.in_app_control_policy import PUBLIC_CONTROL_ACTIONS, decide_control


def test_allowed_tap_and_swipe():
    tap = decide_control({"action": "tap", "x": 100, "y": 200})
    assert tap.allowed and tap.command is not None and tap.command.action == "tap"
    swipe = decide_control({"action": "swipe", "x": 100, "y": 800, "x2": 100, "y2": 400})
    assert swipe.allowed and swipe.command is not None and swipe.command.action == "swipe"
    aliased = decide_control(
        {
            "action": "swipe",
            "start_x": 10,
            "start_y": 20,
            "end_x": 30,
            "end_y": 40,
            "duration_ms": 250,
        }
    )
    assert aliased.allowed and aliased.command is not None
    assert aliased.command.x == 10 and aliased.command.y2 == 40
    assert aliased.command.duration_ms == 250


def test_type_back_home_and_recents_allowed():
    typed = decide_control({"action": "type", "text": "OK"})
    assert typed.allowed and typed.command is not None and typed.command.text == "OK"
    for action in ("back", "home", "recents"):
        decision = decide_control({"action": action})
        assert decision.allowed, action
        assert decision.command is not None and decision.command.action == action
    assert PUBLIC_CONTROL_ACTIONS == ("tap", "swipe", "type", "back", "home", "recents")


def test_setup_mode_rejects_home_and_recents_only():
    assert decide_control({"action": "home"}, setup_mode=True).allowed is False
    assert decide_control({"action": "home"}, setup_mode=True).reason == "forbidden_control"
    assert decide_control({"action": "recents"}, setup_mode=True).allowed is False
    assert decide_control({"action": "back"}, setup_mode=True).allowed is True
    tap = decide_control({"action": "tap", "x": 10, "y": 20}, setup_mode=True)
    assert tap.allowed is True
    assert decide_control({"action": "home"}).allowed is True
    assert decide_control({"action": "recents"}).allowed is True


def test_bottom_edge_upward_swipe_is_normal_navigation():
    gesture = decide_control({"action": "swipe", "x": 720, "y": 2900, "x2": 720, "y2": 2000})
    assert gesture.allowed and gesture.command is not None
    assert gesture.command.action == "swipe"
    assert gesture.command.y == 2900 and gesture.command.y2 == 2000


def test_notification_and_keys_rejected():
    for payload in (
        {"action": "swipe", "x": 100, "y": 10, "x2": 100, "y2": 500},
        {"action": "keyevent", "keycode": 3},
        {"action": "tap", "x": 1, "y": 1, "command": "reboot"},
        {"action": "tap", "x": 1, "y": 1, "udid": "OTHER"},
        {"action": "adb"},
        {"action": "home", "keycode": 3},
        {"action": "shell"},
    ):
        decision = decide_control(payload)
        assert decision.allowed is False, payload
        assert decision.reason == "forbidden_control"


def test_browser_cannot_name_another_device():
    for key, value in (
        ("slot_id", 2),
        ("farm_slot_id", 2),
        ("serial", "X"),
        ("udid", "X"),
        ("workspace_id", "ws"),
    ):
        decision = decide_control({"action": "tap", "x": 1, "y": 1, key: value})
        assert decision.allowed is False
        assert decision.reason == "forbidden_control"


def test_invalid_control_is_not_forbidden():
    missing = decide_control({"action": "tap", "x": 1})
    assert missing.allowed is False and missing.reason == "invalid_control"
    unknown = decide_control({"action": "pinch", "x": 1, "y": 1})
    assert unknown.allowed is False and unknown.reason == "invalid_control"
    bad_duration = decide_control(
        {"action": "swipe", "start_x": 1, "start_y": 2, "end_x": 3, "end_y": 4, "duration_ms": 0}
    )
    assert bad_duration.allowed is False and bad_duration.reason == "invalid_control"
    extra = decide_control({"action": "home", "x": 1})
    assert extra.allowed is False and extra.reason == "invalid_control"
