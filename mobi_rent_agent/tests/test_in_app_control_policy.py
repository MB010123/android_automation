"""Server-side in-app control allowlist."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.in_app_control_policy import decide_control


def test_allowed_tap_and_swipe():
    tap = decide_control({"action": "tap", "x": 100, "y": 200})
    assert tap.allowed and tap.command is not None and tap.command.action == "tap"
    swipe = decide_control({"action": "swipe", "x": 100, "y": 800, "x2": 100, "y2": 400})
    assert swipe.allowed and swipe.command is not None and swipe.command.action == "swipe"


def test_type_and_back_allowed():
    typed = decide_control({"action": "type", "text": "OK"})
    assert typed.allowed and typed.command is not None and typed.command.text == "OK"
    back = decide_control({"action": "back"})
    assert back.allowed and back.command is not None and back.command.action == "back"


def test_home_recents_notification_and_keys_rejected():
    for payload in (
        {"action": "home"},
        {"action": "recents"},
        {"action": "swipe", "x": 100, "y": 10, "x2": 100, "y2": 500},
        {"action": "swipe", "x": 720, "y": 2900, "x2": 720, "y2": 2000},
        {"action": "keyevent", "keycode": 3},
        {"action": "tap", "x": 1, "y": 1, "command": "reboot"},
        {"action": "tap", "x": 1, "y": 1, "udid": "OTHER"},
        {"action": "adb"},
    ):
        decision = decide_control(payload)
        assert decision.allowed is False, payload


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
