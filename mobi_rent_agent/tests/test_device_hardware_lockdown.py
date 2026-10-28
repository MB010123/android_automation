"""Farm hardware lockdown commands: volume, vibrate, camera, mic, flashlight."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.device_hardware_lockdown import (
    AudioLockdownEnforcer,
    audio_lockdown_reassert_commands,
    hardware_lockdown_commands,
    reassert_audio_lockdown,
)
from infrastructure.adb_companion import AdbCommandResult, AdbCommandRunner


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
        activity="com.android.settings/.Settings$SoundSettingsActivity",
        force=True,
        enforcer=gate,
    )
    assert applied is True
    joined = " ".join(" ".join(args) for args in runner.calls)
    assert "mode_ringer 0" in joined
    assert "input keyevent 3" in joined
