"""Persistent hardware lockdowns for farm phones (no Device Owner required).

Volume, vibrate, camera, microphone, and flashlight stay off across boot,
clean/reset, and session start. An on-device shell daemon reasserts mute
every 1s (independent of GADS WebRTC / Farm HTTP) and Homes out of Samsung
``SecSoundSettings`` / ``Sounds and vibration``. Farm Agent only deploys
and respawns that daemon. ``dpm set-user-restriction`` is best-effort and
fails closed on unrooted shells. Never touches eUICC/LPA packages.
"""
from __future__ import annotations

import logging
import os
import tempfile
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
GUARD_CHECK_INTERVAL_SECONDS = 5.0
LOCKDOWN_GUARD_REMOTE = "/data/local/tmp/mobirent_lockdown_guard.sh"
LOCKDOWN_GUARD_PGREP = "mobirent_lockdown_guard.sh"
LOCKDOWN_GUARD_SCRIPT = """#!/system/bin/sh
# On-device mute + Sound settings guard. Independent of GADS/HTTP.
PIDFILE=/data/local/tmp/mobirent_lockdown_guard.pid
if [ -f "$PIDFILE" ]; then
  old=$(cat "$PIDFILE" 2>/dev/null)
  if [ -n "$old" ] && [ "$old" != "$$" ] && kill -0 "$old" 2>/dev/null; then
    cmd=$(tr '\0' ' ' < "/proc/$old/cmdline" 2>/dev/null)
    case "$cmd" in
      *mobirent_lockdown_guard.sh*) exit 0 ;;
    esac
  fi
fi
echo $$ > "$PIDFILE"

enforce_audio() {
  ringer=$(settings get global mode_ringer 2>/dev/null)
  if [ "$ringer" != "0" ]; then
    cmd audio set-ringer-mode SILENT >/dev/null 2>&1
    settings put global mode_ringer 0 >/dev/null 2>&1
  fi
  vibrate=$(settings get system vibrate_on 2>/dev/null)
  if [ "$vibrate" != "0" ]; then
    settings put system vibrate_on 0 >/dev/null 2>&1
    settings put system vibrate_when_ringing 0 >/dev/null 2>&1
  fi
  cmd media_session volume --stream 1 --set 0 >/dev/null 2>&1
  cmd media_session volume --stream 2 --set 0 >/dev/null 2>&1
  cmd media_session volume --stream 3 --set 0 >/dev/null 2>&1
  cmd media_session volume --stream 4 --set 0 >/dev/null 2>&1
  cmd media_session volume --stream 5 --set 0 >/dev/null 2>&1
}

kick_sound_settings() {
  input keyevent 3 >/dev/null 2>&1
  am force-stop com.android.settings >/dev/null 2>&1
}

sound_screen_open() {
  blob=$( { dumpsys window displays; dumpsys activity top; } 2>/dev/null | tr 'A-Z' 'a-z' | tr -d ' \t\r\n' )
  case "$blob" in
    *secsoundsettings*|*soundsettings*|*secvolumesettings*|*volumelimiter*|*vibrationintensity*|*vibrationpattern*|*zenmodesettings*)
      return 0
      ;;
  esac
  case "$blob" in
    *com.android.settings*)
      case "$blob" in
        *soundmode*|*soundsandvibration*)
          return 0
          ;;
      esac
      ;;
  esac
  return 1
}

while true; do
  enforce_audio
  if sound_screen_open; then
    kick_sound_settings
    enforce_audio
  fi
  sleep 1
done
"""

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


def _shell_has_pid(stdout: str) -> bool:
    return any(token.isdigit() for token in (stdout or "").replace("\n", " ").split())


def is_lockdown_guard_running(runner: Any, serial: str) -> bool:
    """True when ``pgrep -f mobirent_lockdown_guard.sh`` finds a live pid."""
    if runner is None:
        return False
    try:
        out = runner.run(serial, ["shell", "pgrep", "-f", LOCKDOWN_GUARD_PGREP]).stdout
        if _shell_has_pid(out):
            return True
    except (AdbCommandError, OSError, TypeError):
        pass
    try:
        out = runner.run(serial, ["shell", "ps", "-A"]).stdout
        return LOCKDOWN_GUARD_PGREP in (out or "")
    except (AdbCommandError, OSError, TypeError):
        return False


def _guard_file_present(runner: Any, serial: str) -> bool:
    try:
        listed = runner.run(serial, ["shell", "ls", LOCKDOWN_GUARD_REMOTE]).stdout or ""
    except (AdbCommandError, OSError, TypeError):
        return False
    return LOCKDOWN_GUARD_REMOTE in listed or LOCKDOWN_GUARD_PGREP in listed


def deploy_lockdown_guard(runner: Any, serial: str) -> bool:
    """Push the 1s guard script to ``/data/local/tmp`` (Unix newlines)."""
    if runner is None:
        return False
    fd, local = tempfile.mkstemp(prefix="mobirent_lockdown_guard_", suffix=".sh")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(LOCKDOWN_GUARD_SCRIPT)
        runner.run(serial, ["push", local, LOCKDOWN_GUARD_REMOTE])
        runner.run(serial, ["shell", "chmod", "755", LOCKDOWN_GUARD_REMOTE])
        return True
    except (AdbCommandError, OSError, TypeError):
        logger.info("lockdown_guard_deploy_skipped serial=%s", serial)
        return False
    finally:
        try:
            os.unlink(local)
        except OSError:
            pass


def start_lockdown_guard_process(runner: Any, serial: str) -> bool:
    """Launch the on-device daemon. ``nohup`` first, plain ``sh ... &`` fallback."""
    if runner is None:
        return False
    launch = (
        "rm -f /data/local/tmp/mobirent_lockdown_guard.pid; "
        f"nohup sh {LOCKDOWN_GUARD_REMOTE} >/dev/null 2>&1 &"
    )
    try:
        runner.run(serial, ["shell", launch])
        return True
    except (AdbCommandError, OSError, TypeError):
        try:
            runner.run(
                serial,
                ["shell", f"sh {LOCKDOWN_GUARD_REMOTE} >/dev/null 2>&1 &"],
            )
            return True
        except (AdbCommandError, OSError, TypeError):
            logger.info("lockdown_guard_start_skipped serial=%s", serial)
            return False


def stop_lockdown_guard_process(runner: Any, serial: str) -> None:
    if runner is None:
        return
    script = (
        f"for p in $(pgrep -f {LOCKDOWN_GUARD_PGREP} 2>/dev/null); "
        'do kill "$p" 2>/dev/null; true; done'
    )
    try:
        runner.run(serial, ["shell", script])
    except (AdbCommandError, OSError, TypeError):
        return


def ensure_on_device_lockdown_guard(
    runner: Any,
    serial: str,
    *,
    redeploy: bool = False,
) -> bool:
    """Deploy/start the on-device 1s guard. Respawn if ``pgrep`` finds nothing."""
    key = str(serial or "").strip()
    if not key or runner is None:
        return False
    if redeploy:
        stop_lockdown_guard_process(runner, key)
        if not deploy_lockdown_guard(runner, key):
            return False
        started = start_lockdown_guard_process(runner, key)
        if started:
            logger.info("lockdown_guard_started serial=%s", key)
        return started
    if is_lockdown_guard_running(runner, key):
        return True
    if not _guard_file_present(runner, key):
        if not deploy_lockdown_guard(runner, key):
            return False
    started = start_lockdown_guard_process(runner, key)
    if started:
        logger.info("lockdown_guard_respawned serial=%s", key)
    return started


def reassert_audio_lockdown(
    runner: Any,
    serial: str,
    *,
    activity: str | None = None,
    dump: str | None = None,
    force: bool = False,
    enforcer: AudioLockdownEnforcer | None = None,
) -> bool:
    """Mute/silent again if the interval elapsed. Home out of Sound settings."""
    from application.session_restriction_policy import (
        is_sound_settings_activity,
        sound_settings_dump_matches,
    )

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
    if is_sound_settings_activity(activity) or sound_settings_dump_matches(dump):
        try:
            if runner is not None:
                runner.run(serial, ["shell", "input", "keyevent", "3"])
                runner.run(serial, ["shell", "am", "force-stop", "com.android.settings"])
            applied = True
        except (AdbCommandError, OSError, TypeError):
            logger.info("sound_settings_home_skipped serial=%s", serial)
    return applied


def start_audio_lockdown_loop(
    *,
    adb_path: str,
    slot_map: dict[int, str],
    interval_seconds: float = GUARD_CHECK_INTERVAL_SECONDS,
    stop_event: threading.Event | None = None,
    command_runner: AdbCommandRunner | None = None,
) -> threading.Thread:
    """Deploy the on-device guard and respawn it when ``pgrep`` finds it dead."""
    stop = stop_event or threading.Event()
    runner = command_runner or AdbCommandRunner(adb_path, timeout_seconds=8.0)

    def _serials() -> list[str]:
        seen: list[str] = []
        for serial in list(slot_map.values()):
            text = str(serial or "").strip()
            if text and text not in seen:
                seen.append(text)
        return seen

    def _loop() -> None:
        first = True
        while not stop.is_set():
            for serial in _serials():
                if stop.is_set():
                    return
                try:
                    ensure_on_device_lockdown_guard(
                        runner, serial, redeploy=first
                    )
                except (AdbCommandError, OSError, TypeError):
                    logger.info("lockdown_guard_loop_skipped serial=%s", serial)
            first = False
            if stop.wait(interval_seconds):
                return

    thread = threading.Thread(target=_loop, name="lockdown-guard", daemon=True)
    thread.start()
    logger.info("lockdown_guard_loop_started interval=%s", interval_seconds)
    return thread
