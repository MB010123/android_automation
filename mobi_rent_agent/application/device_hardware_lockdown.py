"""Persistent hardware lockdowns for farm phones (no Device Owner required).

Volume, vibrate, camera, microphone, and flashlight stay off across boot,
clean/reset, and session start. A lightweight reassert runs while a remote
session is active so Quick Settings / Sound settings cannot leave the phone
on vibrate or with volume up. ``dpm set-user-restriction`` is best-effort
and fails closed on unrooted shells. Never touches eUICC/LPA packages.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from application.remote_access_farm_task import ALL_CAMERA_PACKAGES
from infrastructure.adb_companion import AdbCommandError, AdbCommandRunner

logger = logging.getLogger("farm_agent.hardware_lockdown")

# User-restriction keys (UserManager). Best-effort without device owner.
DPM_NO_ADJUST_VOLUME = "no_adjust_volume"
DPM_NO_UNMUTE_MICROPHONE = "no_unmute_microphone"
DPM_NO_CAMERA = "no_camera"

AUDIO_ENFORCE_INTERVAL_SECONDS = 4.0

VOLUME_MUTE_COMMANDS = (
    "cmd audio set-ringer-mode SILENT",
    "cmd audio set-ringer-mode 0",
    "settings put global mode_ringer 0",
    "cmd media_session volume --stream 1 --set 0",
    "cmd media_session volume --stream 2 --set 0",
    "cmd media_session volume --stream 3 --set 0",
    "cmd media_session volume --stream 4 --set 0",
    "cmd media_session volume --stream 5 --set 0",
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
    "settings put system volume_music_speaker 0",
    "settings put system volume_ring_speaker 0",
    "settings put system volume_notification_speaker 0",
)

VIBRATE_OFF_COMMANDS = (
    "settings put system vibrate_when_ringing 0",
    "settings put system haptic_feedback_enabled 0",
    "settings put system sound_effects_enabled 0",
    "settings put system vibrate_on 0",
    "settings put system ringtone_vibration_sep_index 0",
    "settings put system dtmf_tone 0",
    "settings put system lockscreen_sounds_enabled 0",
    "settings put system apply_ramping_ringer 0",
)

VIBRATE_APPOPS_PACKAGES = (
    "com.android.systemui",
    "com.sec.android.app.launcher",
    "com.google.android.apps.nexuslauncher",
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

# Pixel lowercase plus Samsung One UI names. No SoundMode / flashlight / camera / mic.
QS_TILES_NO_FLASHLIGHT_OR_SOUND = "Wifi,bt,RotationLock,AirplaneMode"


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


def vibrate_appops_commands() -> tuple[str, ...]:
    return tuple(
        f"cmd appops set {package} VIBRATE ignore" for package in VIBRATE_APPOPS_PACKAGES
    )


def audio_lockdown_reassert_commands() -> tuple[str, ...]:
    """Cheap mute/silent set used on the session guard loop."""
    return (
        "cmd audio set-ringer-mode SILENT",
        "cmd audio set-ringer-mode 0",
        "settings put global mode_ringer 0",
        "settings put system vibrate_on 0",
        "settings put system vibrate_when_ringing 0",
        "settings put system haptic_feedback_enabled 0",
        "cmd media_session volume --stream 1 --set 0",
        "cmd media_session volume --stream 2 --set 0",
        "cmd media_session volume --stream 3 --set 0",
        "cmd media_session volume --stream 4 --set 0",
        "cmd media_session volume --stream 5 --set 0",
        "media volume --stream 2 --set 0",
        "media volume --stream 3 --set 0",
        "media volume --stream 5 --set 0",
    )


def hardware_lockdown_commands() -> tuple[str, ...]:
    return (
        *VOLUME_MUTE_COMMANDS,
        *VIBRATE_OFF_COMMANDS,
        *vibrate_appops_commands(),
        *CAMERA_MIC_PRIVACY_COMMANDS,
        *FLASHLIGHT_OFF_COMMANDS,
        *disable_all_camera_commands(),
        f"settings put secure sysui_qs_tiles {QS_TILES_NO_FLASHLIGHT_OR_SOUND}",
        *DPM_RESTRICTION_COMMANDS,
    )


def best_effort_lockdown_script(commands: tuple[str, ...] | list[str]) -> str:
    return "; ".join(f"{command}; true" for command in commands if str(command).strip())


class AudioLockdownEnforcer:
    """Rate-limit silent/mute reassert per serial (default 4s)."""

    def __init__(
        self,
        *,
        interval_seconds: float = AUDIO_ENFORCE_INTERVAL_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._interval = max(1.0, float(interval_seconds))
        self._clock = clock or time.monotonic
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def due(self, serial: str, *, force: bool = False) -> bool:
        key = str(serial or "").strip()
        if not key:
            return False
        now = self._clock()
        with self._lock:
            if not force:
                last = self._last.get(key)
                if last is not None and now - last < self._interval:
                    return False
            self._last[key] = now
            return True


_DEFAULT_ENFORCER = AudioLockdownEnforcer()


def reassert_audio_lockdown(
    runner: Any,
    serial: str,
    *,
    activity: str | None = None,
    force: bool = False,
    enforcer: AudioLockdownEnforcer | None = None,
) -> bool:
    """Mute/silent again if the interval elapsed. Home out of Sound settings."""
    from application.session_restriction_policy import is_sound_settings_activity

    gate = enforcer or _DEFAULT_ENFORCER
    if not gate.due(serial, force=force):
        return False
    script = best_effort_lockdown_script(audio_lockdown_reassert_commands())
    applied = False
    try:
        if runner is not None:
            runner.run(serial, ["shell", script])
        applied = True
    except (AdbCommandError, OSError, TypeError):
        logger.info("audio_lockdown_reassert_skipped serial=%s", serial)
    if is_sound_settings_activity(activity):
        try:
            if runner is not None:
                runner.run(serial, ["shell", "input", "keyevent", "3"])
            applied = True
        except (AdbCommandError, OSError, TypeError):
            logger.info("sound_settings_home_skipped serial=%s", serial)
    return applied


def start_audio_lockdown_loop(
    *,
    adb_path: str,
    slot_map: dict[int, str],
    interval_seconds: float = AUDIO_ENFORCE_INTERVAL_SECONDS,
    stop_event: threading.Event | None = None,
    command_runner: AdbCommandRunner | None = None,
) -> threading.Thread:
    """Reassert mute on mapped serials while the Farm Agent is running."""
    stop = stop_event or threading.Event()
    runner = command_runner or AdbCommandRunner(adb_path, timeout_seconds=8.0)
    enforcer = AudioLockdownEnforcer(interval_seconds=interval_seconds)

    def _loop() -> None:
        while not stop.wait(interval_seconds):
            for serial in list(slot_map.values()):
                if stop.is_set():
                    return
                text = str(serial or "").strip()
                if not text:
                    continue
                reassert_audio_lockdown(runner, text, force=False, enforcer=enforcer)

    thread = threading.Thread(target=_loop, name="audio-lockdown", daemon=True)
    thread.start()
    logger.info("audio_lockdown_loop_started interval=%s", interval_seconds)
    return thread
