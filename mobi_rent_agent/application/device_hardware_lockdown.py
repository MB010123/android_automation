"""Persistent hardware lockdowns for farm phones (no Device Owner required).

Volume, vibrate, camera, microphone, and flashlight stay off across boot,
clean/reset, and session start. ``dpm set-user-restriction`` is best-effort
and fails closed on unrooted shells. Never touches eUICC/LPA packages.
"""
from __future__ import annotations

from application.remote_access_farm_task import ALL_CAMERA_PACKAGES

# User-restriction keys (UserManager). Best-effort without device owner.
DPM_NO_ADJUST_VOLUME = "no_adjust_volume"
DPM_NO_UNMUTE_MICROPHONE = "no_unmute_microphone"
DPM_NO_CAMERA = "no_camera"

VOLUME_MUTE_COMMANDS = (
    "media volume --stream 1 --set 0",
    "media volume --stream 2 --set 0",
    "media volume --stream 3 --set 0",
    "media volume --stream 4 --set 0",
    "media volume --stream 5 --set 0",
    "media volume --stream 6 --set 0",
    "settings put system volume_music 0",
    "settings put system volume_ring 0",
    "settings put system volume_alarm 0",
    "settings put system volume_notification 0",
    "settings put system volume_system 0",
    "settings put system volume_voice 0",
    "settings put global mode_ringer 0",
)

VIBRATE_OFF_COMMANDS = (
    "settings put system vibrate_when_ringing 0",
    "settings put system haptic_feedback_enabled 0",
    "settings put system vibrate_on 0",
)

CAMERA_MIC_PRIVACY_COMMANDS = (
    "cmd sensor_privacy enable 0 1",
    "cmd sensor_privacy enable 0 2",
)

FLASHLIGHT_OFF_COMMANDS = (
    "cmd flashlight disable",
)

DPM_RESTRICTION_COMMANDS = (
    f"dpm set-user-restriction {DPM_NO_ADJUST_VOLUME} true",
    f"dpm set-user-restriction {DPM_NO_UNMUTE_MICROPHONE} true",
    f"dpm set-user-restriction {DPM_NO_CAMERA} true",
)

QS_TILES_NO_FLASHLIGHT_OR_SOUND = "wifi,cell,bt,airplane,rotation,battery,hotspot"


def disable_all_camera_commands() -> tuple[str, ...]:
    commands: list[str] = []
    seen: set[str] = set()
    for package in ALL_CAMERA_PACKAGES:
        if package in seen:
            continue
        seen.add(package)
        commands.append(f"pm disable-user --user 0 {package}")
        commands.append(f"am force-stop {package}")
    return tuple(commands)


def hardware_lockdown_commands() -> tuple[str, ...]:
    return (
        *VOLUME_MUTE_COMMANDS,
        *VIBRATE_OFF_COMMANDS,
        *CAMERA_MIC_PRIVACY_COMMANDS,
        *FLASHLIGHT_OFF_COMMANDS,
        *disable_all_camera_commands(),
        f"settings put secure sysui_qs_tiles {QS_TILES_NO_FLASHLIGHT_OR_SOUND}",
        *DPM_RESTRICTION_COMMANDS,
    )
