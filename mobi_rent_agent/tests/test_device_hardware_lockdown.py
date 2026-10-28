"""Farm hardware lockdown commands: volume, vibrate, camera, mic, flashlight."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.device_hardware_lockdown import (
    LOCKDOWN_GUARD_REMOTE,
    LOCKDOWN_GUARD_SCRIPT,
    AudioLockdownEnforcer,
    audio_lockdown_reassert_commands,
    ensure_on_device_lockdown_guard,
    hardware_lockdown_commands,
    reassert_audio_lockdown,
    start_audio_lockdown_loop,
)
from infrastructure.adb_companion import AdbCommandError, AdbCommandResult, AdbCommandRunner


def test_hardware_lockdown_covers_volume_camera_mic_flashlight():
    joined = " ; ".join(hardware_lockdown_commands())
    assert "cmd audio set-ringer-mode SILENT" in joined
    assert "cmd media_session volume --stream 2 --set 0" in joined
    assert "media volume --stream 3 --set 0" in joined
    assert "settings put global mode_ringer 0" in joined
    assert "vibrate_when_ringing 0" in joined
    assert "haptic_feedback_enabled 0" in joined
    assert "sound_effects_enabled 0" in joined
    assert "vibrate_on 0" in joined
    assert "ringtone_vibration_sep_index 0" in joined
    assert "cmd appops set com.android.systemui VIBRATE ignore" in joined
    assert "cmd sensor_privacy enable 0 1" in joined
    assert "cmd sensor_privacy enable 0 2" in joined
    assert "cmd flashlight disable" in joined
    assert "pm disable-user --user 0 com.google.android.GoogleCamera" in joined
    assert "pm disable-user --user 0 com.sec.android.app.camera" in joined
    assert "dpm set-user-restriction no_adjust_volume true" in joined
    assert "dpm set-user-restriction no_camera true" in joined
    assert "dpm set-user-restriction no_unmute_microphone true" in joined
    tiles = joined.split("sysui_qs_tiles", 1)[-1].lower()
    assert "flashlight" not in tiles
    assert "soundmode" not in tiles
    assert "euicc" not in joined.lower()
    assert "*" not in joined


def test_audio_reassert_is_silent_not_vibrate():
    joined = " ; ".join(audio_lockdown_reassert_commands())
    assert "set-ringer-mode SILENT" in joined
    assert "mode_ringer 0" in joined
    assert "vibrate_on 0" in joined
    assert "cmd media_session volume --stream 5 --set 0" in joined
    assert "mode_ringer 1" not in joined


def test_audio_enforcer_rate_limits_per_serial():
    clock = {"now": 0.0}
    gate = AudioLockdownEnforcer(interval_seconds=4.0, clock=lambda: clock["now"])
    assert gate.due("RFCNB056WEV") is True
    assert gate.due("RFCNB056WEV") is False
    clock["now"] = 3.9
    assert gate.due("RFCNB056WEV") is False
    clock["now"] = 4.0
    assert gate.due("RFCNB056WEV") is True
    assert gate.due("PIXEL-1") is True


class _LockRunner(AdbCommandRunner):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[list[str]] = []

    def run(self, serial: str, arguments: list[str], **_kwargs) -> AdbCommandResult:
        self.calls.append(list(arguments))
        return AdbCommandResult(stdout="", stderr="")


def test_reassert_homes_out_of_samsung_sound_settings():
    runner = _LockRunner()
    gate = AudioLockdownEnforcer(interval_seconds=4.0, clock=lambda: 10.0)
    applied = reassert_audio_lockdown(
        runner,
        "RFCNB056WEV",
        activity="com.android.settings/.Settings$SecSoundSettingsActivity",
        force=True,
        enforcer=gate,
    )
    assert applied is True
    joined = " ".join(" ".join(args) for args in runner.calls)
    assert "mode_ringer 0" in joined
    assert "input keyevent 3" in joined
    assert "force-stop com.android.settings" in joined


def test_reassert_homes_out_of_samsung_sounds_and_vibration_dump():
    runner = _LockRunner()
    gate = AudioLockdownEnforcer(interval_seconds=4.0, clock=lambda: 10.0)
    applied = reassert_audio_lockdown(
        runner,
        "RFCNB056WEV",
        activity="com.android.settings/.SubSettings",
        dump=(
            "mCurrentFocus=Window{abc u0 com.android.settings/.SubSettings}\n"
            'text="Sounds and vibration"'
        ),
        force=True,
        enforcer=gate,
    )
    assert applied is True
    joined = " ".join(" ".join(args) for args in runner.calls)
    assert "input keyevent 3" in joined
    assert "force-stop com.android.settings" in joined


def test_on_device_guard_script_enforces_silent_and_kicks_sec_sound():
    script = LOCKDOWN_GUARD_SCRIPT
    assert "settings get global mode_ringer" in script
    assert "cmd audio set-ringer-mode SILENT" in script
    assert "settings put global mode_ringer 0" in script
    assert "settings get system vibrate_on" in script
    assert "settings put system vibrate_on 0" in script
    assert "cmd media_session volume --stream 3 --set 0" in script
    assert "dumpsys window displays" in script
    assert "dumpsys activity top" in script
    assert "secsoundsettings" in script
    assert "soundsandvibration" in script
    assert "input keyevent 3" in script
    assert "am force-stop com.android.settings" in script
    assert "sleep 1" in script
    assert "euicc" not in script.lower()
    assert "lpa" not in script.lower()


class _GuardRunner(AdbCommandRunner):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[list[str]] = []
        self.running = False

    def run(self, serial: str, arguments: list[str], **_kwargs) -> AdbCommandResult:
        self.calls.append(list(arguments))
        joined = " ".join(arguments)
        if arguments[:2] == ["shell", "pgrep"]:
            return AdbCommandResult(stdout="4321" if self.running else "", stderr="")
        if arguments[:2] == ["shell", "ls"]:
            stdout = LOCKDOWN_GUARD_REMOTE if self.running else ""
            return AdbCommandResult(stdout=stdout, stderr="")
        if arguments[:2] == ["shell", "ps"]:
            stdout = f"4321 sh {LOCKDOWN_GUARD_REMOTE}" if self.running else ""
            return AdbCommandResult(stdout=stdout, stderr="")
        if arguments[0] == "push" or "nohup sh" in joined:
            self.running = True
        if "kill" in joined and "pgrep -f" in joined:
            self.running = False
        return AdbCommandResult(stdout="", stderr="")


def test_ensure_guard_pushes_chmod_and_nohup():
    runner = _GuardRunner()
    assert ensure_on_device_lockdown_guard(runner, "RFCNB056WEV", redeploy=True) is True
    joined = " ".join(" ".join(args) for args in runner.calls)
    assert any(args and args[0] == "push" for args in runner.calls)
    assert LOCKDOWN_GUARD_REMOTE in joined
    assert "chmod 755" in joined
    assert f"nohup sh {LOCKDOWN_GUARD_REMOTE}" in joined


def test_ensure_guard_skips_redeploy_when_pgrep_alive():
    runner = _GuardRunner()
    runner.running = True
    assert ensure_on_device_lockdown_guard(runner, "RFCNB056WEV", redeploy=False) is True
    assert not any(args and args[0] == "push" for args in runner.calls)
    assert any(args[:2] == ["shell", "pgrep"] for args in runner.calls)


def test_lockdown_loop_deploys_then_respawns_via_pgrep():
    stop = threading.Event()
    runner = _GuardRunner()
    thread = start_audio_lockdown_loop(
        adb_path="adb",
        slot_map={21: "RFCNB056WEV"},
        interval_seconds=0.05,
        stop_event=stop,
        command_runner=runner,
    )
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if any(args and args[0] == "push" for args in runner.calls) and any(
            args[:2] == ["shell", "pgrep"] for args in runner.calls
        ):
            break
        time.sleep(0.02)
    stop.set()
    thread.join(2.0)
    joined = " ".join(" ".join(args) for args in runner.calls)
    assert LOCKDOWN_GUARD_REMOTE in joined
    assert "pgrep" in joined
    assert f"nohup sh {LOCKDOWN_GUARD_REMOTE}" in joined


S20_SERIAL = "RFCNB056WEV"
_SEC_SOUND = (
    "com.android.settings/.Settings$SecSoundSettingsActivity"
)


def _s20_online() -> bool:
    try:
        state = AdbCommandRunner("adb", timeout_seconds=5.0).run(
            S20_SERIAL, ["get-state"]
        ).stdout
    except Exception:
        return False
    return str(state or "").strip() == "device"


def _focus_lines(stdout: str) -> str:
    lines = []
    for line in str(stdout or "").lower().splitlines():
        if "mcurrentfocus" in line or "mfocusedapp" in line or "mresumedactivity" in line:
            lines.append(line)
    return "\n".join(lines)


@pytest.mark.skipif(not _s20_online(), reason="Samsung S20 Slot 21 is not on ADB")
def test_live_s20_guard_kicks_sounds_and_vibration_and_holds_mute():
    runner = AdbCommandRunner("adb", timeout_seconds=12.0)
    assert ensure_on_device_lockdown_guard(runner, S20_SERIAL, redeploy=True) is True
    time.sleep(1.0)
    try:
        pids = runner.run(
            S20_SERIAL, ["shell", "pgrep", "-f", "mobirent_lockdown_guard.sh"]
        ).stdout
    except AdbCommandError:
        pids = ""
    assert any(token.isdigit() for token in pids.replace("\n", " ").split()), pids

    runner.run(
        S20_SERIAL,
        ["shell", "am", "start", "-a", "android.settings.SOUND_SETTINGS"],
    )
    try:
        runner.run(S20_SERIAL, ["shell", "am", "start", "-n", _SEC_SOUND])
    except AdbCommandError:
        pass
    time.sleep(1.5)
    focus = _focus_lines(
        runner.run(S20_SERIAL, ["shell", "dumpsys", "window", "displays"]).stdout
    )
    assert "secsoundsettings" not in focus, focus
    assert "soundsettings" not in focus, focus
    assert "launcher" in focus, focus
    top = runner.run(S20_SERIAL, ["shell", "dumpsys", "activity", "top"]).stdout.lower()
    top_head = "\n".join(top.splitlines()[:40])
    assert "secsoundsettings" not in top_head, top_head

    runner.run(S20_SERIAL, ["shell", "settings", "put", "global", "mode_ringer", "1"])
    runner.run(S20_SERIAL, ["shell", "settings", "put", "system", "vibrate_on", "1"])
    time.sleep(1.5)
    ringer = runner.run(
        S20_SERIAL, ["shell", "settings", "get", "global", "mode_ringer"]
    ).stdout.strip()
    vibrate = runner.run(
        S20_SERIAL, ["shell", "settings", "get", "system", "vibrate_on"]
    ).stdout.strip()
    assert ringer == "0", ringer
    assert vibrate == "0", vibrate

    runner.run(S20_SERIAL, ["shell", "am", "start", "-n", "com.android.settings/.Settings"])
    time.sleep(1.5)
    settings_focus = _focus_lines(
        runner.run(S20_SERIAL, ["shell", "dumpsys", "window", "displays"]).stdout
    )
    assert "secsoundsettings" not in settings_focus, settings_focus
    assert "sounds and vibration" not in settings_focus, settings_focus
