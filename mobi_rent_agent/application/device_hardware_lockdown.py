"""Persistent hardware lockdowns for farm phones (no Device Owner required).

Volume, vibrate, camera, microphone, and flashlight stay off across boot,
clean/reset, and session start. An on-device 1s daemon keeps the renter on
Phone/SMS/eSIM, Homes blocked apps, and kicks sensitive Settings pages.
Farm Agent only deploys and respawns that daemon. Never touches eUICC/LPA.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from application.remote_access_farm_task import ALL_CAMERA_PACKAGES
from application.session_restriction_policy import (
    HIDDEN_USER_PACKAGES,
    NEVER_FORCE_STOP_PACKAGES,
    PICKER_ONLY_PACKAGES,
    PICKER_OR_ESIM_MARKERS,
    RENTER_ALLOWED_PACKAGES,
    RENTER_SETTINGS_PACKAGE,
    SENSITIVE_SETTINGS_DUMP_MARKERS,
)
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
ADMIN_MODE_FLAG = "/data/local/tmp/mobirent_admin_mode"
DEVICE_ACCESS_MODE_TASK = "device_access_mode"


def _shell_or_pattern(values: frozenset[str] | tuple[str, ...] | set[str]) -> str:
    items = [str(item).strip() for item in values if str(item).strip()]
    return "|".join(sorted(set(items)))


def _shell_case_globs(values: tuple[str, ...] | frozenset[str]) -> str:
    items = [str(item).strip().lower().replace(" ", "") for item in values if str(item).strip()]
    return "|".join(f"*{item}*" for item in sorted(set(items)))


def build_lockdown_guard_script() -> str:
    """Unix-newline shell for the on-device 1s Phone/SMS/eSIM allowlist guard."""
    permission = _shell_or_pattern(
        {
            "com.google.android.permissioncontroller",
            "com.android.permissioncontroller",
        }
    )
    allowed = _shell_or_pattern(
        RENTER_ALLOWED_PACKAGES
        - {RENTER_SETTINGS_PACKAGE}
        - PICKER_ONLY_PACKAGES
        - {
            "com.google.android.permissioncontroller",
            "com.android.permissioncontroller",
        }
    )
    never = _shell_or_pattern(NEVER_FORCE_STOP_PACKAGES - {""})
    picker = _shell_or_pattern(PICKER_ONLY_PACKAGES)
    picker_blob = _shell_case_globs(PICKER_OR_ESIM_MARKERS)
    sensitive = _shell_case_globs(SENSITIVE_SETTINGS_DUMP_MARKERS)
    settings_pkg = RENTER_SETTINGS_PACKAGE
    template = r"""#!/system/bin/sh
# On-device Phone/SMS/eSIM allowlist + mute. Independent of GADS/HTTP.
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

extract_pkg() {
  line=$(echo "$1" | tr 'A-Z' 'a-z')
  rest="$line"
  case "$line" in
    *' u0 '*) rest=${line#*' u0 '} ;;
    *' u10 '*) rest=${line#*' u10 '} ;;
  esac
  pkg=${rest%%/*}
  pkg=${pkg%% *}
  pkg=${pkg%%\}*}
  case "$pkg" in
    ""|null|none|mcurrentfocus=null) echo "" ;;
    *) echo "$pkg" ;;
  esac
}

kick_pkg() {
  pkg=$1
  case "$pkg" in
    ""|__NEVER__) return 0 ;;
  esac
  am force-stop "$pkg" >/dev/null 2>&1
  input keyevent 3 >/dev/null 2>&1
}

sensitive_settings() {
  blob=$1
  case "$blob" in
    __SENSITIVE__) return 0 ;;
  esac
  return 1
}

picker_context() {
  blob=$1
  case "$blob" in
    __PICKER_BLOB__) return 0 ;;
  esac
  return 1
}

while true; do
  if [ -f /data/local/tmp/mobirent_admin_mode ]; then sleep 1; continue; fi
  enforce_audio
  focus=$(dumpsys window 2>/dev/null | grep -i mCurrentFocus | grep -v '=null' | tail -n 1)
  pkg=$(extract_pkg "$focus")
  case "$pkg" in
    ""|__NEVER__|__ALLOWED__)
      ;;
    __SETTINGS__)
      top=$(dumpsys activity top 2>/dev/null | tr 'A-Z' 'a-z' | tr -d '[:space:]')
      if sensitive_settings "$top"; then
        kick_pkg __SETTINGS__
      fi
      ;;
    __PERMISSION__)
      top=$(dumpsys activity top 2>/dev/null | tr 'A-Z' 'a-z' | tr -d '[:space:]')
      if sensitive_settings "$top"; then
        kick_pkg "$pkg"
      fi
      ;;
    __PICKER__)
      top=$(dumpsys activity top 2>/dev/null | tr 'A-Z' 'a-z' | tr -d '[:space:]')
      if picker_context "$top"; then
        :
      else
        kick_pkg "$pkg"
      fi
      ;;
    *)
      kick_pkg "$pkg"
      ;;
  esac
  sleep 1
done
"""
    return (
        template.replace("__NEVER__", never)
        .replace("__ALLOWED__", allowed)
        .replace("__SENSITIVE__", sensitive)
        .replace("__PICKER_BLOB__", picker_blob)
        .replace("__PICKER__", picker)
        .replace("__SETTINGS__", settings_pkg)
        .replace("__PERMISSION__", permission)
    )


LOCKDOWN_GUARD_SCRIPT = build_lockdown_guard_script()

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
# Restored while /data/local/tmp/mobirent_admin_mode exists. Guard stays alive.
ADMIN_QS_TILES = "Wifi,bt,Dnd,Flashlight,RotationLock,AirplaneMode,SoundMode"


def hidden_user_app_commands() -> tuple[str, ...]:
    """Disable Play Store, Chrome, YouTube, and other non-Phone/SMS apps."""
    commands: list[str] = []
    seen: set[str] = set()
    for package in HIDDEN_USER_PACKAGES:
        if package in seen:
            continue
        seen.add(package)
        commands.append(f"pm disable-user --user 0 {package}")
        commands.append(f"am force-stop {package}")
    return tuple(commands)


def admin_unlock_commands() -> tuple[str, ...]:
    """Re-enable hidden apps and restore Quick Settings while admin mode is on."""
    commands: list[str] = [
        f"settings put secure sysui_qs_tiles {ADMIN_QS_TILES}",
        f"dpm set-user-restriction {DPM_NO_ADJUST_VOLUME} false",
        f"dpm set-user-restriction {DPM_NO_UNMUTE_MICROPHONE} false",
        f"dpm set-user-restriction {DPM_NO_CAMERA} false",
    ]
    seen: set[str] = set()
    for package in HIDDEN_USER_PACKAGES:
        if package in seen:
            continue
        seen.add(package)
        commands.append(f"pm enable --user 0 {package}")
    return tuple(commands)


def parse_admin_flag(value: Any) -> bool | None:
    """Parse `true`/`false` from query strings, JSON booleans, or 1/0."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    return None


def resolve_admin_mode(
    *,
    query: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    default: bool | None = None,
) -> bool | None:
    """Query `?admin=` wins over JSON `{"admin": ...}`; else `default`."""
    raw: Any = None
    if query:
        listed = query.get("admin")
        if isinstance(listed, (list, tuple)):
            raw = listed[0] if listed else None
        elif listed is not None:
            raw = listed
        parsed = parse_admin_flag(raw)
        if parsed is not None:
            return parsed
    if isinstance(body, dict) and "admin" in body:
        parsed = parse_admin_flag(body.get("admin"))
        if parsed is not None:
            return parsed
    return default


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
        *hidden_user_app_commands(),
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
    """True when a live ``sh /data/local/tmp/mobirent_lockdown_guard.sh`` pid exists."""
    if runner is None:
        return False
    if _guard_pids(runner, serial):
        return True
    try:
        out = runner.run(serial, ["shell", "pgrep", "-f", LOCKDOWN_GUARD_PGREP]).stdout
        if _shell_has_pid(out):
            return True
    except (AdbCommandError, OSError, TypeError):
        pass
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


def _guard_pids(runner: Any, serial: str) -> list[str]:
    """PIDs whose cmdline is the on-device guard script (not a wrapper shell)."""
    try:
        out = runner.run(serial, ["shell", "ps", "-A", "-o", "PID,ARGS"]).stdout or ""
    except (AdbCommandError, OSError, TypeError):
        return []
    pids: list[str] = []
    for line in (out or "").splitlines():
        if LOCKDOWN_GUARD_PGREP not in line:
            continue
        if "grep" in line:
            continue
        for token in line.split():
            if token.isdigit():
                pids.append(token)
                break
    return pids


def start_lockdown_guard_process(runner: Any, serial: str) -> bool:
    """Launch the on-device daemon. ``nohup`` first, plain ``sh ... &`` fallback."""
    if runner is None:
        return False
    stop_lockdown_guard_process(runner, serial)
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
    for pid in _guard_pids(runner, serial):
        try:
            runner.run(serial, ["shell", "kill", "-9", pid])
        except (AdbCommandError, OSError, TypeError):
            continue


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


def is_device_admin_mode(runner: Any, serial: str) -> bool:
    """True when `/data/local/tmp/mobirent_admin_mode` exists on the phone."""
    key = str(serial or "").strip()
    if not key or runner is None:
        return False
    script = f"if [ -f {ADMIN_MODE_FLAG} ]; then echo 1; fi"
    try:
        out = runner.run(key, ["shell", script]).stdout or ""
    except (AdbCommandError, OSError, TypeError):
        return False
    return "1" in out


def set_device_admin_mode(
    runner: Any,
    serial: str,
    admin: bool,
    *,
    ensure_guard: bool = True,
    apply_policy: bool = True,
) -> bool:
    """Touch or remove the admin-mode flag. The 1s daemon stays running either way.

    ``apply_policy=True`` (API / live access) restores Quick Settings and hidden
    apps for admin, or re-applies client hardware lockdown. Automated farm
    tasks pass ``apply_policy=False`` so they only pause kicks.
    """
    key = str(serial or "").strip()
    if not key or runner is None:
        return False
    if ensure_guard:
        try:
            ensure_on_device_lockdown_guard(runner, key, redeploy=False)
        except (AdbCommandError, OSError, TypeError):
            logger.info("lockdown_guard_ensure_skipped serial=%s", key)
    if admin:
        try:
            runner.run(key, ["shell", f"touch {ADMIN_MODE_FLAG}"])
        except (AdbCommandError, OSError, TypeError):
            logger.info("admin_mode_touch_skipped serial=%s", key)
            return False
        if apply_policy:
            script = best_effort_lockdown_script(admin_unlock_commands())
            try:
                if script:
                    runner.run(key, ["shell", script])
            except (AdbCommandError, OSError, TypeError):
                logger.info("admin_mode_unlock_skipped serial=%s", key)
        logger.info("admin_mode_enabled serial=%s", key)
        return True
    try:
        runner.run(key, ["shell", f"rm -f {ADMIN_MODE_FLAG}"])
    except (AdbCommandError, OSError, TypeError):
        logger.info("admin_mode_clear_skipped serial=%s", key)
        return False
    if apply_policy:
        script = best_effort_lockdown_script(hardware_lockdown_commands())
        try:
            if script:
                runner.run(key, ["shell", script])
        except (AdbCommandError, OSError, TypeError):
            logger.info("admin_mode_lockdown_skipped serial=%s", key)
    logger.info("admin_mode_disabled serial=%s", key)
    return True


def apply_payload_admin_mode(runner: Any, serial: str, payload: dict[str, Any] | None) -> bool | None:
    """Apply `payload.admin` when present. Returns the parsed flag or None."""
    admin = parse_admin_flag((payload or {}).get("admin")) if isinstance(payload, dict) else None
    if admin is None:
        return None
    set_device_admin_mode(runner, serial, admin)
    return admin


@contextmanager
def pause_renter_lockdown(runner: Any, serial: str) -> Iterator[None]:
    """Temporarily set admin mode so the 1s guard does not interrupt ADB automation."""
    previous = is_device_admin_mode(runner, serial)
    set_device_admin_mode(runner, serial, True, ensure_guard=False, apply_policy=False)
    try:
        yield
    finally:
        set_device_admin_mode(
            runner, serial, previous, ensure_guard=True, apply_policy=False
        )


def run_device_access_mode(
    *,
    adb_path: str,
    slot_map: dict[int, str],
    request: Any,
    command_runner: AdbCommandRunner | None = None,
) -> Any:
    """Farm task: flip one slot between admin (full access) and client lockdown."""
    from application.farm_task_types import FarmTaskResult
    from domain.farm import MAX_FARM_SLOT_ID

    slot_id = int(request.farm_slot_id)
    if not 1 <= slot_id <= MAX_FARM_SLOT_ID:
        return FarmTaskResult(ok=False, http_status=404, error="slot_not_found")
    serial = str(slot_map.get(slot_id) or "").strip()
    if not serial:
        return FarmTaskResult(ok=False, http_status=404, error="slot_not_found")
    payload = request.payload if isinstance(request.payload, dict) else {}
    admin = parse_admin_flag(payload.get("admin"))
    if admin is None:
        return FarmTaskResult(ok=False, http_status=400, error="invalid_admin")
    runner = command_runner or AdbCommandRunner(adb_path, timeout_seconds=8.0)
    try:
        state = str(runner.run(serial, ["get-state"]).stdout or "").strip()
    except AdbCommandError:
        state = ""
    if state != "device":
        return FarmTaskResult(ok=False, http_status=409, error="device_offline")
    set_device_admin_mode(runner, serial, admin)
    return FarmTaskResult(
        ok=True,
        http_status=200,
        details={
            "ok": True,
            "slot_id": slot_id,
            "serial": serial,
            "admin": admin,
            "guard_running": is_lockdown_guard_running(runner, serial),
        },
    )


def request_device_access_mode(farm_client: Any, slot_id: int, *, admin: bool) -> None:
    """Best-effort VPS → Farm admin-mode flip. Never raises."""
    if farm_client is None:
        return
    try:
        farm_client.run_task(
            task_type=DEVICE_ACCESS_MODE_TASK,
            farm_slot_id=int(slot_id),
            payload={"admin": bool(admin)},
            job_id=f"access-mode-{int(slot_id)}-{uuid.uuid4().hex[:8]}",
        )
    except Exception:
        logger.warning("device_access_mode_request_failed slot=%s admin=%s", slot_id, admin)


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
        package_from_activity,
        renter_should_kick_foreground,
    )

    gate = enforcer or _DEFAULT_ENFORCER
    if not gate.due(serial, force=force):
        return False
    if is_device_admin_mode(runner, serial):
        return False
    script = best_effort_lockdown_script(audio_lockdown_reassert_commands())
    applied = False
    try:
        if runner is not None:
            runner.run(serial, ["shell", script])
        applied = True
    except (AdbCommandError, OSError, TypeError):
        logger.info("audio_lockdown_reassert_skipped serial=%s", serial)
    pkg = package_from_activity(activity)
    if renter_should_kick_foreground(pkg, dump=dump, activity=activity):
        stop_pkg = pkg or "com.android.settings"
        try:
            if runner is not None:
                runner.run(serial, ["shell", "input", "keyevent", "3"])
                if stop_pkg not in NEVER_FORCE_STOP_PACKAGES:
                    runner.run(serial, ["shell", "am", "force-stop", stop_pkg])
            applied = True
        except (AdbCommandError, OSError, TypeError):
            logger.info("renter_allowlist_home_skipped serial=%s", serial)
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
