"""Per-screen dashboard instruction bubble (screen_guidance)."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.device_hardware_lockdown import (
    ADMIN_MODE_FLAG,
    LOCKDOWN_GUARD_SCRIPT,
    SCREEN_STATE_REMOTE,
    ensure_on_device_lockdown_guard,
)
from application.farm_task_executor import FarmTaskExecutorDeps, execute_controlled_farm_task
from application.farm_task_types import FarmTaskRequest, SUPPORTED_TASK_TYPES
from application.screen_guidance import (
    cached_device_screen_guidance,
    clear_screen_guidance_caches,
    parse_screen_state_line,
    public_screen_guidance,
    run_device_screen_guidance,
)
from application.screen_guidance_catalog import (
    SCREEN_KEY_ABOUT_PHONE,
    SCREEN_KEY_BLOCKED_KICK,
    SCREEN_KEY_DIALER,
    SCREEN_KEY_ESIM_QR_FLOW,
    SCREEN_KEY_HOME,
    SCREEN_KEY_MESSAGES,
    SCREEN_KEY_SETTINGS_MAIN,
    SCREEN_KEY_SETTINGS_NETWORK,
    SCREEN_KEYS,
    guidance_for,
)
from application.samsung_one_ui import samsung_settings_args, samsung_sim_manager_args
from application.vps_lovable_routes import parse_route
from infrastructure.adb_companion import AdbCommandError, AdbCommandResult, AdbCommandRunner

S20_SERIAL = "RFCNB056WEV"
S20_SLOT = 21


class FakeRunner(AdbCommandRunner):
    def __init__(self, state_line: str = "home||0") -> None:
        super().__init__()
        self.state_line = state_line
        self.calls: list[tuple[str, list[str]]] = []

    def run(self, serial: str, arguments: list[str]) -> AdbCommandResult:
        self.calls.append((serial, arguments))
        if arguments[:3] == ["shell", "cat", SCREEN_STATE_REMOTE]:
            return AdbCommandResult(stdout=self.state_line, stderr="")
        if arguments[:2] == ["shell", "getprop"]:
            if arguments[-1] == "ro.product.manufacturer":
                return AdbCommandResult(stdout="samsung", stderr="")
            if arguments[-1] == "ro.product.model":
                return AdbCommandResult(stdout="SM-G981U", stderr="")
        return AdbCommandResult(stdout="", stderr="")


def test_guard_script_writes_screen_state_and_blocked_kick():
    script = LOCKDOWN_GUARD_SCRIPT
    assert SCREEN_STATE_REMOTE in script
    assert "blocked_kick" in script
    assert "maybe_record" in script
    assert "kick_and_record" in script
    assert "classify_focus" in script
    assert "telephonyui" in script
    assert "settings_network" in script
    assert "esim_qr_flow" in script
    assert "about_phone" in script
    assert "blocked_sound" in script
    assert "blocked_security" in script
    assert "blocked_reset" in script
    assert "if [ -f /data/local/tmp/mobirent_admin_mode ]; then sleep 1; continue; fi" in script
    assert "sleep 0.4" in script
    assert "dumpsys window displays" in script
    assert "is_settings_home" in script
    assert "settingshomepage" in script
    # Atomic state write: readers never see a torn line.
    assert f'> "$STATE.tmp" && mv -f "$STATE.tmp" "$STATE"' in script
    # Hot path order inside the loop: focus read happens before enforce_audio.
    loop = script[script.index("while true; do") :]
    assert loop.index("dumpsys window displays") < loop.index("enforce_audio")


def test_screen_guidance_coalesces_concurrent_polls_and_keeps_last_known():
    import threading

    clear_screen_guidance_caches()

    class SlowRunner(FakeRunner):
        def run(self, serial: str, arguments: list[str]) -> AdbCommandResult:  # noqa: D401
            if arguments[:3] == ["shell", "cat", SCREEN_STATE_REMOTE]:
                time.sleep(0.15)
            return super().run(serial, arguments)

    runner = SlowRunner("dialer||0")
    results: list[str] = []

    def poll() -> None:
        results.append(
            cached_device_screen_guidance(runner, S20_SERIAL, slot_id=S20_SLOT)["screen_key"]
        )

    threads = [threading.Thread(target=poll) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    cats = sum(1 for _serial, args in runner.calls if args[:2] == ["shell", "cat"])
    assert cats == 1, runner.calls
    assert results == [SCREEN_KEY_DIALER] * 8

    # Empty / timed-out cat => last known state, never a dumpsys fallback.
    time.sleep(0.45)
    runner.state_line = ""
    again = cached_device_screen_guidance(runner, S20_SERIAL, slot_id=S20_SLOT)
    assert again["screen_key"] == SCREEN_KEY_DIALER
    joined = " ".join(" ".join(args) for _serial, args in runner.calls)
    assert "dumpsys" not in joined
    assert "getprop" not in joined


def test_catalog_covers_every_screen_key_with_do_and_dont():
    for key in SCREEN_KEYS:
        for family in ("s20", "pixel"):
            body = guidance_for(key, device_family=family, blocked_reason="blocked_sound")
            assert body["screen_key"] == key
            assert body["title"]
            assert body["badge"]
            assert body["severity"] in {"info", "success", "warning"}
            assert body["do_list"]
            assert body["dont_list"]
            assert body["device_family"] == family
    blocked = guidance_for(
        SCREEN_KEY_BLOCKED_KICK, device_family="s20", blocked_reason="blocked_sound"
    )
    assert blocked["severity"] == "warning"
    assert blocked["title"] == "Restricted Screen Blocked"
    assert blocked["blocked_reason"] == "blocked_sound"
    s20 = guidance_for(SCREEN_KEY_SETTINGS_NETWORK, device_family="s20")
    pixel = guidance_for(SCREEN_KEY_SETTINGS_NETWORK, device_family="pixel")
    assert "SIM manager" in s20["do_list"][0]
    assert "SIMs" in pixel["do_list"][0]


def test_catalog_source_has_no_device_control_verbs():
    source = (ROOT / "application" / "screen_guidance_catalog.py").read_text(encoding="utf-8")
    assert "force-stop" not in source
    assert "pm disable" not in source


def test_parse_screen_state_line_and_blocked_ttl():
    assert parse_screen_state_line("dialer||0") == ("dialer", "")
    assert parse_screen_state_line("blocked_kick|blocked_sound|9999999999")[0] == "blocked_kick"
    key, reason = parse_screen_state_line(
        "blocked_kick|blocked_sound|10", now=100.0
    )
    assert key == SCREEN_KEY_HOME
    assert reason == ""
    assert parse_screen_state_line("nope||0")[0] == SCREEN_KEY_HOME


def test_device_screen_guidance_task_maps_state_file():
    clear_screen_guidance_caches()
    runner = FakeRunner("settings_network||0")
    result = run_device_screen_guidance(
        adb_path="adb",
        slot_map={S20_SLOT: S20_SERIAL},
        request=FarmTaskRequest(
            job_id="g",
            task_type="device_screen_guidance",
            farm_slot_id=S20_SLOT,
            payload={},
        ),
        command_runner=runner,
    )
    assert result.ok is True
    guidance = result.details["screen_guidance"]
    assert guidance["screen_key"] == SCREEN_KEY_SETTINGS_NETWORK
    assert guidance["do_list"]
    assert guidance["dont_list"]
    assert "device_screen_guidance" in SUPPORTED_TASK_TYPES
    clear_screen_guidance_caches()
    dispatched = execute_controlled_farm_task(
        adb_path="adb",
        slot_map={S20_SLOT: S20_SERIAL},
        request=FarmTaskRequest(
            job_id="g2",
            task_type="device_screen_guidance",
            farm_slot_id=S20_SLOT,
            payload={},
        ),
        deps=FarmTaskExecutorDeps(command_runner=FakeRunner("messages||0")),
    )
    assert dispatched.details["screen_guidance"]["screen_key"] == SCREEN_KEY_MESSAGES


def test_screen_guidance_cache_avoids_repeat_adb():
    clear_screen_guidance_caches()
    runner = FakeRunner("dialer||0")
    first = cached_device_screen_guidance(runner, S20_SERIAL, slot_id=S20_SLOT)
    cats = sum(1 for _serial, args in runner.calls if args[:2] == ["shell", "cat"])
    second = cached_device_screen_guidance(runner, S20_SERIAL, slot_id=S20_SLOT)
    cats_after = sum(1 for _serial, args in runner.calls if args[:2] == ["shell", "cat"])
    assert first["screen_key"] == SCREEN_KEY_DIALER
    assert second["screen_key"] == SCREEN_KEY_DIALER
    assert cats_after == cats
    joined = " ".join(" ".join(args) for _serial, args in runner.calls)
    assert "dumpsys" not in joined
    assert "getprop" not in joined


def test_rental_and_farm_screen_guidance_routes():
    rental = "00000000-0000-4000-8000-000000000021"
    customer = parse_route(f"/rentals/{rental}/screen-guidance")
    assert customer is not None
    assert customer.kind == "rental_screen_guidance"
    assert customer.rental_id == rental
    farm = parse_route("/farm/slots/21/screen-guidance")
    assert farm is not None
    assert farm.kind == "farm_screen_guidance"
    assert farm.farm_bay == 21


def test_public_payload_shape():
    body = public_screen_guidance(screen_key=SCREEN_KEY_HOME, device_family="s20")
    assert set(body) >= {"screen_key", "title", "badge", "severity", "do_list", "dont_list"}
    for key in (
        SCREEN_KEY_HOME,
        SCREEN_KEY_DIALER,
        SCREEN_KEY_MESSAGES,
        SCREEN_KEY_SETTINGS_MAIN,
        SCREEN_KEY_SETTINGS_NETWORK,
        SCREEN_KEY_ESIM_QR_FLOW,
        SCREEN_KEY_ABOUT_PHONE,
        SCREEN_KEY_BLOCKED_KICK,
    ):
        item = public_screen_guidance(screen_key=key, device_family="s20")
        assert isinstance(item["do_list"], list)
        assert isinstance(item["dont_list"], list)


def _serial_online(serial: str) -> bool:
    try:
        state = AdbCommandRunner("adb", timeout_seconds=5.0).run(serial, ["get-state"]).stdout
    except Exception:
        return False
    return str(state or "").strip() == "device"


def _cat_state(runner: AdbCommandRunner, serial: str) -> str:
    try:
        return (runner.run(serial, ["shell", "cat", SCREEN_STATE_REMOTE]).stdout or "").strip()
    except AdbCommandError:
        return ""


def _wait_state(runner: AdbCommandRunner, serial: str, prefix: str, *, timeout: float = 4.0) -> str:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = _cat_state(runner, serial)
        if last.startswith(prefix):
            return last
        time.sleep(0.4)
    return last


@pytest.mark.skipif(not _serial_online(S20_SERIAL), reason="Samsung S20 Slot 21 is not on ADB")
def test_live_s20_screen_guidance_home_dialer_messages_network_and_blocked():
    runner = AdbCommandRunner("adb", timeout_seconds=12.0)
    try:
        runner.run(S20_SERIAL, ["shell", f"rm -f {ADMIN_MODE_FLAG}"])
    except AdbCommandError:
        pass
    assert ensure_on_device_lockdown_guard(runner, S20_SERIAL, redeploy=True) is True
    runner.run(S20_SERIAL, ["shell", "input keyevent KEYCODE_WAKEUP; true; wm dismiss-keyguard; true"])
    clear_screen_guidance_caches()

    runner.run(S20_SERIAL, ["shell", "input", "keyevent", "3"])
    home_line = _wait_state(runner, S20_SERIAL, "home")
    home = cached_device_screen_guidance(runner, S20_SERIAL, slot_id=S20_SLOT)
    assert home["screen_key"] == SCREEN_KEY_HOME, home_line
    assert home["do_list"] and home["dont_list"]

    runner.run(S20_SERIAL, ["shell", "am", "start", "-a", "android.intent.action.DIAL"])
    dial_line = _wait_state(runner, S20_SERIAL, "dialer")
    clear_screen_guidance_caches()
    dial = cached_device_screen_guidance(runner, S20_SERIAL, slot_id=S20_SLOT)
    assert dial["screen_key"] == SCREEN_KEY_DIALER, dial_line
    assert "keypad" in " ".join(dial["do_list"]).lower()

    runner.run(
        S20_SERIAL,
        [
            "shell",
            "am",
            "start",
            "-a",
            "android.intent.action.MAIN",
            "-c",
            "android.intent.category.APP_MESSAGING",
        ],
    )
    sms_line = _wait_state(runner, S20_SERIAL, "messages")
    clear_screen_guidance_caches()
    sms = cached_device_screen_guidance(runner, S20_SERIAL, slot_id=S20_SLOT)
    assert sms["screen_key"] == SCREEN_KEY_MESSAGES, sms_line

    runner.run(S20_SERIAL, ["shell", *samsung_settings_args()])
    settings_line = _wait_state(runner, S20_SERIAL, "settings_", timeout=4.0)
    runner.run(S20_SERIAL, ["shell", *samsung_sim_manager_args()])
    net_line = _wait_state(runner, S20_SERIAL, "settings_network", timeout=5.0)
    if not net_line.startswith("settings_network"):
        net_line = _wait_state(runner, S20_SERIAL, "esim_qr_flow", timeout=3.0)
    clear_screen_guidance_caches()
    net = cached_device_screen_guidance(runner, S20_SERIAL, slot_id=S20_SLOT)
    assert net["screen_key"] in {
        SCREEN_KEY_SETTINGS_NETWORK,
        SCREEN_KEY_SETTINGS_MAIN,
        SCREEN_KEY_ESIM_QR_FLOW,
    }, f"settings={settings_line!r} sim={net_line!r}"
    assert net["do_list"] and net["dont_list"]

    runner.run(S20_SERIAL, ["shell", "am", "start", "-a", "android.settings.SOUND_SETTINGS"])
    kick_line = _wait_state(runner, S20_SERIAL, "blocked_kick", timeout=5.0)
    clear_screen_guidance_caches()
    kick = cached_device_screen_guidance(runner, S20_SERIAL, slot_id=S20_SLOT)
    assert kick["screen_key"] == SCREEN_KEY_BLOCKED_KICK, kick_line
    assert kick["severity"] == "warning"
    assert kick.get("blocked_reason") in {
        "blocked_sound",
        "blocked_app",
        "blocked_security",
        "blocked_reset",
    }
    runner.run(S20_SERIAL, ["shell", "input", "keyevent", "3"])
