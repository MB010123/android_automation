"""Farm hardware lockdown commands: volume, vibrate, camera, mic, flashlight."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.device_hardware_lockdown import hardware_lockdown_commands


def test_hardware_lockdown_covers_volume_camera_mic_flashlight():
    joined = " ; ".join(hardware_lockdown_commands())
    assert "media volume --stream 3 --set 0" in joined
    assert "settings put global mode_ringer 0" in joined
    assert "vibrate_when_ringing 0" in joined
    assert "haptic_feedback_enabled 0" in joined
    assert "cmd sensor_privacy enable 0 1" in joined
    assert "cmd sensor_privacy enable 0 2" in joined
    assert "cmd flashlight disable" in joined
    assert "pm disable-user --user 0 com.google.android.GoogleCamera" in joined
    assert "pm disable-user --user 0 com.sec.android.app.camera" in joined
    assert "dpm set-user-restriction no_adjust_volume true" in joined
    assert "dpm set-user-restriction no_camera true" in joined
    assert "dpm set-user-restriction no_unmute_microphone true" in joined
    tiles = joined.split("sysui_qs_tiles", 1)[-1]
    assert "flashlight" not in tiles
    assert "euicc" not in joined.lower()
    assert "*" not in joined
