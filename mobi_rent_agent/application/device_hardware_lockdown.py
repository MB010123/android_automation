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

# The on-device guard owns mute + kicks. Host-side reasserts are a slow safety
# net only: they spawn `adb shell` processes that compete with GADS' persistent
# ADB sockets on the 21-bay hub, so keep them rare.
AUDIO_ENFORCE_INTERVAL_SECONDS = 30.0
# Respawn sweep: one `ps` per phone. 60s is plenty for a daemon that only dies
# on reboot / adbd restart; session start and access-mode flips re-check anyway.
GUARD_CHECK_INTERVAL_SECONDS = 60.0
GUARD_TICK_SECONDS = 0.4
LOCKDOWN_GUARD_REMOTE = "/data/local/tmp/mobirent_lockdown_guard.sh"
LOCKDOWN_GUARD_PGREP = "mobirent_lockdown_guard.sh"
ADMIN_MODE_FLAG = "/data/local/tmp/mobirent_admin_mode"
SCREEN_STATE_REMOTE = "/data/local/tmp/mobirent_screen_state"
SCREEN_STATE_KICK_SECONDS = 6
DEVICE_ACCESS_MODE_TASK = "device_access_mode"

# USB replug / reboot popups. One UI 5 ships the "Allow access to phone data?"
# dialog in com.samsung.android.mtp (older builds: com.samsung.android.MtpApplication)
# and the USB-mode sheet in com.sec.usbsettings. The AOSP app-level USB
# permission dialogs live in com.android.systemui.
SAMSUNG_MTP_PACKAGES = (
    "com.samsung.android.mtp",
    "com.samsung.android.MtpApplication",
)
SAMSUNG_USB_SETTINGS_PACKAGE = "com.sec.usbsettings"
USB_POPUP_PACKAGES = (*SAMSUNG_MTP_PACKAGES, SAMSUNG_USB_SETTINGS_PACKAGE)
# Dismissed with BACK by the guard. UsbDebuggingActivity (the RSA "Allow USB
# debugging?" prompt) is deliberately NOT here: BACK there means Deny, which
# would cut ADB for the bay. adb_allowed_connection_time=0 keeps it from
# re-appearing on an authorised host.
USB_POPUP_ACTIVITY_MARKERS = (
    "usbconfirmactivity",
    "usbpermissionactivity",
    "usbaccessoryuriactivity",
)

ALLOWED_SETTINGS_ACTIVITY_MARKERS = (
    "connectionssettings",
    "networkdashboard",
    "mobilenetwork",
    "simsettings",
    "wirelesssettings",
    "wifisettings",
    "tethersettings",
    "airplane",
    "euicc",
    "mydeviceinfo",
    "deviceinfosettings",
    "aboutdevice",
    "aboutphone",
    "statusactivity",
    "settingshomepage",
)


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
    # Gallery / Photos / My Files are always allowed for the eSIM QR picker so
    # the hot path never needs `dumpsys activity top` to confirm picker context.
    allowed = _shell_or_pattern(
        (RENTER_ALLOWED_PACKAGES | PICKER_ONLY_PACKAGES)
        - {RENTER_SETTINGS_PACKAGE}
        - {
            "com.google.android.permissioncontroller",
            "com.android.permissioncontroller",
        }
    )
    never = _shell_or_pattern(NEVER_FORCE_STOP_PACKAGES - {""})
    sensitive = _shell_case_globs(SENSITIVE_SETTINGS_DUMP_MARKERS)
    allowed_settings = _shell_case_globs(ALLOWED_SETTINGS_ACTIVITY_MARKERS)
    settings_pkg = RENTER_SETTINGS_PACKAGE
    # extract_pkg lowercases the focus, so match lowercase package names.
    usb_popup_pkgs = _shell_or_pattern(tuple(pkg.lower() for pkg in USB_POPUP_PACKAGES))
    usb_popup_acts = _shell_case_globs(USB_POPUP_ACTIVITY_MARKERS)
    template = r"""#!/system/bin/sh
# On-device Phone/SMS/eSIM allowlist + mute. Independent of GADS/HTTP.
# Hot path per 0.4s tick: one `dumpsys window displays` focus grep, then the
# state file. `settings get` audio checks every 10 ticks; `dumpsys activity top`
# only for com.android.settings/.SubSettings.
PIDFILE=/data/local/tmp/mobirent_lockdown_guard.pid
# Single instance: the newest launch wins. One `ps` pass; kill any other live
# guard whose ARGS is exactly `sh <script>` (never the nohup wrapper shell).
# Second pass 0.5s later catches a sibling that was still mid-exec.
kill_siblings() {
  ps -A -o PID,ARGS 2>/dev/null | while read -r p rest; do
    if [ "$rest" = "sh /data/local/tmp/mobirent_lockdown_guard.sh" ] && [ "$p" != "$$" ]; then
      kill -9 "$p" 2>/dev/null
    fi
  done
}
kill_siblings
echo $$ > "$PIDFILE"
sleep 0.5
kill_siblings

mute_streams() {
  cmd media_session volume --stream 1 --set 0 >/dev/null 2>&1
  cmd media_session volume --stream 2 --set 0 >/dev/null 2>&1
  cmd media_session volume --stream 3 --set 0 >/dev/null 2>&1
  cmd media_session volume --stream 4 --set 0 >/dev/null 2>&1
  cmd media_session volume --stream 5 --set 0 >/dev/null 2>&1
}

apply_mute() {
  cmd audio set-ringer-mode SILENT >/dev/null 2>&1
  settings put global mode_ringer 0 >/dev/null 2>&1
  settings put system vibrate_on 0 >/dev/null 2>&1
  settings put system vibrate_when_ringing 0 >/dev/null 2>&1
  mute_streams
}

enforce_audio() {
  ringer=$(settings get global mode_ringer 2>/dev/null)
  vibrate=$(settings get system vibrate_on 2>/dev/null)
  if [ "$ringer" != "0" ] || [ "$vibrate" != "0" ]; then
    apply_mute
  fi
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

extract_act() {
  line=$(echo "$1" | tr 'A-Z' 'a-z' | tr '$' '.')
  rest="$line"
  case "$line" in
    *' u0 '*) rest=${line#*' u0 '} ;;
    *' u10 '*) rest=${line#*' u10 '} ;;
  esac
  comp=${rest%% *}
  comp=${comp%%\}*}
  act=${comp#*/}
  echo "$act"
}

kick_pkg() {
  pkg=$1
  case "$pkg" in
    ""|__NEVER__) return 0 ;;
    *.*) ;;
    *) return 0 ;;
  esac
  am force-stop "$pkg" >/dev/null 2>&1
  input keyevent 3 >/dev/null 2>&1
}

is_settings_home() {
  act=$1
  case "$act" in
    .settings|settings|com.android.settings.settings|*settingshomepage*)
      return 0 ;;
  esac
  return 1
}

allowed_settings_act() {
  act=$1
  case "$act" in
    __ALLOWED_SETTINGS__) return 0 ;;
  esac
  return 1
}

forbidden_reason() {
  blob=$1
  case "$blob" in
    *secsound*|*soundsettings*|*soundsandvibration*|*volumesettings*|*secvolume*|*zenmode*|*soundmode*|*vibrationintensity*)
      echo blocked_sound; return 0 ;;
    *securitysettings*|*securityadvanced*|*securitydashboard*|*biometric*|*chooselock*|*safetycenter*|*fingerprint*|*facesettings*|*privacysettings*|*privacydashboard*|*lockscreenmenu*|*screenlock*)
      echo blocked_security; return 0 ;;
    *resetdashboard*|*resetsettings*|*secreset*|*generalmanagement*|*masterclear*|*factoryreset*|*eraseesim*)
      echo blocked_reset; return 0 ;;
    *notificationsettings*|*configurenotification*|*appnotificationsettings*|*accountdashboard*|*accountssettings*|*addaccount*|*backupsettings*|*backupandrestore*)
      echo blocked_app; return 0 ;;
    __SENSITIVE__)
      echo blocked_app; return 0 ;;
  esac
  echo ""
  return 1
}

classify_focus() {
  pkg=$1
  act=$2
  case "$pkg" in
    *nexuslauncher*|*sec.android.app.launcher*|*launcher3*|*samsung.android.launcher*)
      echo home; return ;;
    *dialer*|*incallui*|*com.android.phone*|*com.android.server.telecom*)
      echo dialer; return ;;
    *messaging*|*mms*)
      echo messages; return ;;
    *euicc*|*documentsui*|*photospicker*|*gallery*|*photos*|*myfiles*|*providers.media*)
      echo esim_qr_flow; return ;;
    *telephonyui*)
      echo settings_network; return ;;
    *settings*)
      case "$act" in
        *mydeviceinfo*|*deviceinfosettings*|*aboutdevice*|*aboutphone*|*statusactivity*)
          echo about_phone; return ;;
        *connectionssettings*|*networkdashboard*|*mobilenetwork*|*simsettings*|*wirelesssettings*|*wifisettings*|*tethersettings*|*euicc*|*airplane*)
          echo settings_network; return ;;
        *)
          echo settings_main; return ;;
      esac
      ;;
  esac
  echo home
}

STATE=/data/local/tmp/mobirent_screen_state
cur_line=""
kick_until=0
tick=0

write_state() {
  # Atomic: readers (`cat`) never see a half-written line.
  printf '%s\n' "$1" > "$STATE.tmp" && mv -f "$STATE.tmp" "$STATE"
  cur_line=$1
}

kick_and_record() {
  reason=$1
  pkg=$2
  now=$(date +%s)
  kick_until=$((now + 6))
  write_state "blocked_kick|${reason}|${kick_until}"
  kick_pkg "$pkg"
}

maybe_record() {
  key=$1
  line="${key}||0"
  [ "$line" = "$cur_line" ] && return 0
  if [ "$kick_until" != "0" ]; then
    now=$(date +%s)
    [ "$now" -lt "$kick_until" ] && return 0
    kick_until=0
  fi
  write_state "$line"
}

subsettings_reason() {
  # Only the fragment / back-stack entries of the SubSettings task. Never the
  # view hierarchy text (menu titles like "Sounds and vibration" live there).
  frag=$(dumpsys activity top 2>/dev/null | sed -n '/ACTIVITY com.android.settings\/.SubSettings/,/ACTIVITY [a-z]/p' | grep -E '#[0-9]+: [A-Za-z0-9_.$]+\{' | head -n 12 | tr 'A-Z' 'a-z' | tr -d '[:space:]')
  forbidden_reason "$frag"
}

# USB replug / reboot popups ("Allow access to phone data?", USB mode sheet,
# app-level USB permission). Dismissed in BOTH admin and client mode, before
# the admin check. The RSA USB-debugging prompt is left alone: BACK = Deny.
dismiss_usb_popup() {
  case "$1" in
    __USB_POPUP_PKGS__)
      am force-stop "$1"
      input keyevent 4
      return 0
      ;;
  esac
  case "$2" in
    __USB_POPUP_ACTS__)
      input keyevent 4
      return 0
      ;;
  esac
  return 1
}

apply_mute
write_state "home||0"

while true; do
  # Hot path first: one focus read, then the state file is written by the
  # classify/kick branches below BEFORE any slower work (audio every ~4s).
  focus=$(dumpsys window displays 2>/dev/null | grep -E 'mCurrentFocus|mFocusedApp' | grep -v '=null' | tail -n 1)
  pkg=$(extract_pkg "$focus")
  act=$(extract_act "$focus")
  if dismiss_usb_popup "$pkg" "$act"; then sleep 0.4; continue; fi
  if [ -f /data/local/tmp/mobirent_admin_mode ]; then sleep 1; continue; fi
  case "$pkg" in
    "")
      ;;
    __NEVER__)
      ;;
    __ALLOWED__)
      maybe_record "$(classify_focus "$pkg" "$act")"
      ;;
    __PERMISSION__)
      case "$act" in
        *safetycenter*)
          kick_and_record blocked_security "$pkg"
          ;;
        *)
          maybe_record "$(classify_focus "$pkg" "$act")"
          ;;
      esac
      ;;
    __SETTINGS__)
      if is_settings_home "$act"; then
        maybe_record settings_main
      elif allowed_settings_act "$act"; then
        maybe_record "$(classify_focus "$pkg" "$act")"
      else
        reason=$(forbidden_reason "$act")
        if [ -n "$reason" ]; then
          kick_and_record "$reason" __SETTINGS__
        else
          case "$act" in
            *subsettings*)
              reason=$(subsettings_reason)
              if [ -n "$reason" ]; then
                kick_and_record "$reason" __SETTINGS__
              else
                maybe_record settings_network
              fi
              ;;
            *)
              kick_and_record blocked_app __SETTINGS__
              ;;
          esac
        fi
      fi
      ;;
    *)
      kick_and_record blocked_app "$pkg"
      ;;
  esac
  tick=$((tick + 1))
  if [ "$tick" -ge 10 ]; then
    tick=0
    enforce_audio
  fi
  sleep 0.4
done
"""
    return (
        template.replace("__NEVER__", never)
        .replace("__ALLOWED_SETTINGS__", allowed_settings)
        .replace("__ALLOWED__", allowed)
        .replace("__SENSITIVE__", sensitive)
        .replace("__SETTINGS__", settings_pkg)
        .replace("__PERMISSION__", permission)
        .replace("__USB_POPUP_PKGS__", usb_popup_pkgs)
        .replace("__USB_POPUP_ACTS__", usb_popup_acts)
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


def usb_popup_suppression_commands() -> tuple[str, ...]:
    """Settings that stop USB replug / reboot popups and the ADB auth timer.

    * ``adb_allowed_connection_time 0`` disables the 7-day USB-debugging
      revocation so the farm host's RSA key never expires.
    * ``usb_mass_storage_enabled 0`` + ``usb_audio_automatic_routing_disabled 1``
      keep the gadget from advertising storage/audio roles.
    * ``stay_on_while_plugged_in 7`` + the persist.adb props keep the screen
      and adbd up after a reboot on USB power (props are best-effort: the shell
      user may not own them on every build).
    * Samsung MTP app stays ENABLED. One UI's boot-time gadget composition
      (``mtp,conn_gadget,adb``) needs ``com.samsung.android.mtp`` to finish USB
      enumeration; with the package disabled a warm reboot leaves the S20
      stuck un-enumerated until the cable is physically replugged. We only
      drop its overlay permission; the on-device guard force-stops it the
      moment its dialog takes focus (after boot). ``pm enable`` here repairs
      phones that were disabled by the previous build.
    * ``com.sec.usbsettings`` keeps running (it owns the USB notification
      state) but loses its overlay permission.
    """
    return (
        "settings put global adb_allowed_connection_time 0",
        "settings put global usb_mass_storage_enabled 0",
        "settings put secure usb_audio_automatic_routing_disabled 1",
        "settings put global stay_on_while_plugged_in 7",
        "settings put global adb_enabled 1",
        "setprop persist.adb.ysa 1",
        "setprop persist.service.adb.enable 1",
        *(f"cmd appops set {pkg} SYSTEM_ALERT_WINDOW ignore" for pkg in USB_POPUP_PACKAGES),
        *(f"pm enable {pkg}" for pkg in SAMSUNG_MTP_PACKAGES),
    )


def usb_config_has_mtp(config: str) -> bool:
    parts = {part.strip().lower() for part in str(config or "").split(",")}
    return bool(parts & {"mtp", "ptp"})


def apply_usb_popup_suppression(
    runner: Any,
    serial: str,
    *,
    set_functions: bool = False,
) -> bool:
    """Apply :func:`usb_popup_suppression_commands` on one phone (best-effort).

    ``set_functions=True`` additionally tries ``svc usb setFunctions adb`` when
    ``sys.usb.config`` still carries mtp/ptp. That call re-configures the USB
    gadget at runtime (can re-enumerate on AOSP, is killed by One UI with
    rc 137). Nothing in the Farm Agent passes it any more - reconfiguring the
    gadget is exactly what wedges adbd on the S20 - it stays available for
    explicit operator use only.
    """
    key = str(serial or "").strip()
    if not key or runner is None:
        return False
    script = best_effort_lockdown_script(usb_popup_suppression_commands())
    try:
        runner.run(key, ["shell", script])
    except (AdbCommandError, OSError, TypeError):
        logger.info("usb_popup_suppression_skipped serial=%s", key)
        return False
    if set_functions:
        try:
            config = runner.run(key, ["shell", "getprop", "sys.usb.config"]).stdout or ""
        except (AdbCommandError, OSError, TypeError):
            config = ""
        if usb_config_has_mtp(config):
            try:
                runner.run(key, ["shell", "svc usb setFunctions adb"])
            except (AdbCommandError, OSError, TypeError):
                logger.info("usb_set_functions_rejected serial=%s", key)
    logger.info("usb_popup_suppression_applied serial=%s", key)
    return True


ADB_RESPONSIVE_TIMEOUT_SECONDS = 5.0
ADB_UNRESPONSIVE_ERROR = "device_adb_unresponsive"
USB_REPLUG_HINT = "adbd does not answer shell streams; USB replug or port reset required"


def adb_shell_responsive(
    runner: Any, serial: str, *, timeout_seconds: float = ADB_RESPONSIVE_TIMEOUT_SECONDS
) -> bool:
    """One cheap ``shell echo`` with a short timeout.

    ``adb get-state`` is answered by the host server and says ``device`` even
    when adbd on the phone no longer services streams (One UI after a bad USB
    gadget re-init). Every later ``shell`` would then hang to its timeout, so
    callers probe once and fail fast with :data:`ADB_UNRESPONSIVE_ERROR`.
    """
    key = str(serial or "").strip()
    if not key or runner is None:
        return False
    try:
        try:
            runner.run(key, ["shell", "echo", "mobirent_ok"], timeout_seconds=timeout_seconds)
        except TypeError:
            runner.run(key, ["shell", "echo", "mobirent_ok"])
    except (AdbCommandError, OSError):
        return False
    return True


def _wait_boot_completed_quick(
    runner: Any, serial: str, *, timeout_seconds: float, poll_seconds: float, stop: threading.Event
) -> bool:
    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    while not stop.is_set() and time.monotonic() < deadline:
        try:
            boot = str(runner.run(serial, ["shell", "getprop", "sys.boot_completed"]).stdout or "").strip()
        except (AdbCommandError, OSError, TypeError):
            boot = ""
        if boot == "1":
            return True
        stop.wait(poll_seconds)
    return False


RECONNECT_BOOT_WAIT_SECONDS = 150.0
RECONNECT_POLL_SECONDS = 2.0
RECONNECT_WAKE_COMMANDS = (
    "settings put global stay_on_while_plugged_in 7",
    "input keyevent KEYCODE_WAKEUP",
    "wm dismiss-keyguard",
    "input keyevent 82",
)


def recover_device_after_reconnect(
    runner: Any,
    serial: str,
    *,
    stop: threading.Event | None = None,
    boot_wait_seconds: float = RECONNECT_BOOT_WAIT_SECONDS,
    poll_seconds: float = RECONNECT_POLL_SECONDS,
) -> dict[str, Any]:
    """Post-reboot / replug recovery for one phone.

    Waits for ``sys.boot_completed=1``, then wakes the screen, dismisses the
    keyguard, restarts the on-device guard and re-pins USB popup suppression.
    Never raises; the result dict is for logs/tests.
    """
    key = str(serial or "").strip()
    stop = stop or threading.Event()
    result: dict[str, Any] = {"serial": key, "booted": False, "responsive": False, "guard": False}
    if not key or runner is None:
        return result
    started = time.monotonic()
    result["booted"] = _wait_boot_completed_quick(
        runner, key, timeout_seconds=boot_wait_seconds, poll_seconds=poll_seconds, stop=stop
    )
    if result["booted"]:
        while not stop.is_set() and time.monotonic() - started < boot_wait_seconds:
            if adb_shell_responsive(runner, key):
                result["responsive"] = True
                break
            stop.wait(poll_seconds)
    result["boot_wait_seconds"] = round(time.monotonic() - started, 1)
    if not result["booted"]:
        logger.warning("device_reconnect_boot_timeout serial=%s", key)
        return result
    if not result["responsive"]:
        logger.warning("device_reconnect_adb_unresponsive serial=%s hint=%s", key, USB_REPLUG_HINT)
        return result
    try:
        runner.run(key, ["shell", best_effort_lockdown_script(RECONNECT_WAKE_COMMANDS)])
    except (AdbCommandError, OSError, TypeError):
        logger.info("device_reconnect_wake_skipped serial=%s", key)
    try:
        result["guard"] = bool(ensure_on_device_lockdown_guard(runner, key, redeploy=True))
    except (AdbCommandError, OSError, TypeError):
        result["guard"] = False
    try:
        apply_usb_popup_suppression(runner, key, set_functions=False)
    except (AdbCommandError, OSError, TypeError):
        pass
    logger.info(
        "device_reconnect_recovered serial=%s boot_wait=%ss guard=%s",
        key,
        result["boot_wait_seconds"],
        result["guard"],
    )
    return result


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
    # The script kills any surviving sibling itself, so a missed `ps` sweep
    # can never leave two guards polling the same phone.
    launch = f"nohup sh {LOCKDOWN_GUARD_REMOTE} >/dev/null 2>&1 &"
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
    reassert: bool = False,
) -> bool:
    """Touch or remove the admin-mode flag. The guard daemon stays running either way.

    ``apply_policy=True`` (API / live access) restores Quick Settings and hidden
    apps for admin, or re-applies client hardware lockdown. Automated farm
    tasks pass ``apply_policy=False`` so they only pause kicks.

    Policy commands (``pm disable-user`` / ``am force-stop`` / tiles) only run on
    an actual mode *transition* unless ``reassert`` is set. Re-opening a stream
    on a phone that is already in the requested mode is then two cheap ADB
    calls and never disturbs an active GADS session.
    """
    key = str(serial or "").strip()
    if not key or runner is None:
        return False
    if ensure_guard:
        try:
            ensure_on_device_lockdown_guard(runner, key, redeploy=False)
        except (AdbCommandError, OSError, TypeError):
            logger.info("lockdown_guard_ensure_skipped serial=%s", key)
    if apply_policy and not reassert:
        apply_policy = is_device_admin_mode(runner, key) != bool(admin)
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
        return FarmTaskResult(
            ok=False,
            http_status=409,
            error="device_offline",
            details={
                "ok": False,
                "slot_id": slot_id,
                "serial": serial,
                "hint": USB_REPLUG_HINT,
            },
        )
    if not adb_shell_responsive(runner, serial):
        logger.warning("device_access_mode_adb_unresponsive slot=%s serial=%s", slot_id, serial)
        return FarmTaskResult(
            ok=False,
            http_status=409,
            error=ADB_UNRESPONSIVE_ERROR,
            details={"ok": False, "slot_id": slot_id, "serial": serial, "hint": USB_REPLUG_HINT},
        )
    # Explicit API flips reassert policy; implicit customer open/stream flips
    # (`reassert: false`) only act on a real transition.
    reassert = parse_admin_flag(payload.get("reassert"))
    set_device_admin_mode(
        runner, serial, admin, reassert=True if reassert is None else bool(reassert)
    )
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


def request_device_access_mode(
    farm_client: Any, slot_id: int, *, admin: bool, reassert: bool = True
) -> None:
    """Best-effort VPS → Farm admin-mode flip. Never raises."""
    if farm_client is None:
        return
    payload: dict[str, Any] = {"admin": bool(admin)}
    if not reassert:
        payload["reassert"] = False
    try:
        farm_client.run_task(
            task_type=DEVICE_ACCESS_MODE_TASK,
            farm_slot_id=int(slot_id),
            payload=payload,
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


RECONNECT_POLL_INTERVAL_SECONDS = 5.0


def adb_device_states(adb_path: str, *, timeout_seconds: float = 10.0) -> dict[str, str] | None:
    """``adb devices`` → ``{serial: state}``; ``None`` when the host server failed."""
    import subprocess

    try:
        proc = subprocess.run(
            [adb_path, "devices"], capture_output=True, text=True, timeout=timeout_seconds, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    states: dict[str, str] = {}
    for line in proc.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            states[parts[0]] = parts[1]
    return states


def start_audio_lockdown_loop(
    *,
    adb_path: str,
    slot_map: dict[int, str],
    interval_seconds: float = GUARD_CHECK_INTERVAL_SECONDS,
    stop_event: threading.Event | None = None,
    command_runner: AdbCommandRunner | None = None,
    device_states: Callable[[], dict[str, str] | None] | None = None,
    reconnect_poll_seconds: float = RECONNECT_POLL_INTERVAL_SECONDS,
    recover: Callable[..., Any] | None = None,
) -> threading.Thread:
    """Deploy the on-device guard, respawn it when ``ps`` finds it dead, and
    recover phones that come back from a reboot / USB replug.

    Two cadences on one thread: every ``reconnect_poll_seconds`` a single
    host-side ``adb devices`` (no per-phone stream) looks for serials that
    went missing/offline → ``device``; each such phone gets
    :func:`recover_device_after_reconnect` on its own short-lived thread
    (boot wait, wake, keyguard, guard, popup suppression). Every
    ``interval_seconds`` the usual guard sweep runs for all phones.
    """
    stop = stop_event or threading.Event()
    runner = command_runner or AdbCommandRunner(adb_path, timeout_seconds=8.0)
    states_fn = device_states or (lambda: adb_device_states(adb_path))
    recover_fn = recover or recover_device_after_reconnect
    recovering: set[str] = set()
    recovering_lock = threading.Lock()

    def _serials() -> list[str]:
        seen: list[str] = []
        for serial in list(slot_map.values()):
            text = str(serial or "").strip()
            if text and text not in seen:
                seen.append(text)
        return seen

    def _recover(serial: str) -> None:
        try:
            recover_fn(runner, serial, stop=stop)
        except Exception:  # noqa: BLE001 - daemon thread must never die loudly
            logger.exception("device_reconnect_recover_failed serial=%s", serial)
        finally:
            with recovering_lock:
                recovering.discard(serial)

    def _poll_reconnects(previous: dict[str, str]) -> dict[str, str]:
        states = states_fn()
        if states is None:
            return previous
        current = {serial: states.get(serial, "missing") for serial in _serials()}
        for serial, state in current.items():
            before = previous.get(serial)
            if state == "device" and before is not None and before != "device":
                with recovering_lock:
                    if serial in recovering:
                        continue
                    recovering.add(serial)
                logger.info("device_reconnect_detected serial=%s previous=%s", serial, before)
                threading.Thread(
                    target=_recover, args=(serial,), name=f"reconnect-{serial}", daemon=True
                ).start()
        return current

    def _loop() -> None:
        first = True
        previous: dict[str, str] = {}
        next_sweep = 0.0
        while not stop.is_set():
            now = time.monotonic()
            if now >= next_sweep:
                for serial in _serials():
                    if stop.is_set():
                        return
                    try:
                        ensure_on_device_lockdown_guard(runner, serial, redeploy=first)
                        if first:
                            # Farm Agent startup: pin USB/ADB popup suppression
                            # (settings + MTP re-enable) on every connected phone.
                            apply_usb_popup_suppression(runner, serial, set_functions=False)
                    except (AdbCommandError, OSError, TypeError):
                        logger.info("lockdown_guard_loop_skipped serial=%s", serial)
                first = False
                next_sweep = time.monotonic() + interval_seconds
            previous = _poll_reconnects(previous)
            if stop.wait(min(reconnect_poll_seconds, interval_seconds)):
                return

    thread = threading.Thread(target=_loop, name="lockdown-guard", daemon=True)
    thread.start()
    logger.info("lockdown_guard_loop_started interval=%s", interval_seconds)
    return thread
