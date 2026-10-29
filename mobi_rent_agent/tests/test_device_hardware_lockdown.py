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
    ADMIN_MODE_FLAG,
    LOCKDOWN_GUARD_REMOTE,
    LOCKDOWN_GUARD_SCRIPT,
    SCREEN_STATE_REMOTE,
    AudioLockdownEnforcer,
    audio_lockdown_reassert_commands,
    ensure_on_device_lockdown_guard,
    hardware_lockdown_commands,
    parse_admin_flag,
    reassert_audio_lockdown,
    resolve_admin_mode,
    run_device_access_mode,
    set_device_admin_mode,
    start_audio_lockdown_loop,
)
from application.farm_task_types import FarmTaskRequest, SUPPORTED_TASK_TYPES
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
    assert "pm disable-user --user 0 com.android.chrome" in joined
    assert "pm disable-user --user 0 com.android.vending" in joined
    assert "pm disable-user --user 0 com.google.android.youtube" in joined
    assert "pm disable-user --user 0 com.google.android.documentsui" not in joined
    assert "pm disable-user --user 0 com.sec.android.gallery3d" not in joined
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
    assert "apply_mute" in script
    assert "dumpsys window displays" in script
    assert "mFocusedApp" in script
    assert "settingshomepage" in script
    assert "connectionssettings" in script
    assert "subsettings" in script
    assert "dumpsys activity top" in script
    assert "secsoundsettings" in script
    assert "soundsandvibration" in script
    assert "securitysettings" in script
    assert "generalmanagement" in script
    assert "com.google.android.dialer" in script
    assert "com.google.android.apps.messaging" in script
    assert "com.google.android.euicc" in script
    assert "com.sec.android.gallery3d" in script
    assert "input keyevent 3" in script
    assert "am force-stop" in script
    assert "sleep 0.4" in script
    assert 'mv -f "$STATE.tmp" "$STATE"' in script
    # Single-instance takeover is done on-device (one `ps` pass, exact ARGS
    # match) so a missed host-side kill sweep can never leave two guards.
    assert "kill_siblings" in script
    assert '[ "$rest" = "sh /data/local/tmp/mobirent_lockdown_guard.sh" ]' in script
    assert "if [ -f /data/local/tmp/mobirent_admin_mode ]; then sleep 1; continue; fi" in script
    assert "euiccmanager" not in script.lower()
    assert "wipe_esims" not in script.lower()


def test_parse_admin_flag_query_and_body():
    assert parse_admin_flag(True) is True
    assert parse_admin_flag(False) is False
    assert parse_admin_flag("true") is True
    assert parse_admin_flag("FALSE") is False
    assert parse_admin_flag("1") is True
    assert parse_admin_flag("0") is False
    assert parse_admin_flag(None) is None
    assert resolve_admin_mode(query={"admin": ["true"]}, body={"admin": False}, default=False) is True
    assert resolve_admin_mode(query={}, body={"admin": False}, default=True) is False
    assert resolve_admin_mode(query={}, body={}, default=True) is True
    assert "device_access_mode" in SUPPORTED_TASK_TYPES


def test_set_admin_mode_touches_and_clears_flag():
    runner = _GuardRunner()
    runner.running = True
    assert set_device_admin_mode(runner, "RFCNB056WEV", True) is True
    joined = " ".join(" ".join(args) for args in runner.calls)
    assert f"touch {ADMIN_MODE_FLAG}" in joined
    assert "sysui_qs_tiles" in joined
    assert "pm enable --user 0 com.android.chrome" in joined
    runner.calls.clear()
    assert set_device_admin_mode(runner, "RFCNB056WEV", False) is True
    joined = " ".join(" ".join(args) for args in runner.calls)
    assert f"rm -f {ADMIN_MODE_FLAG}" in joined
    assert "pm disable-user --user 0 com.android.chrome" in joined


def test_set_admin_mode_without_transition_skips_policy_churn():
    """Re-opening a stream on a phone already in client mode must not re-run
    pm disable / am force-stop (that is what disconnects live GADS sessions)."""
    runner = _GuardRunner()
    runner.running = True
    runner.admin_flag = False
    assert set_device_admin_mode(runner, "RFCNB056WEV", False) is True
    joined = " ".join(" ".join(args) for args in runner.calls)
    assert f"rm -f {ADMIN_MODE_FLAG}" in joined
    assert "pm disable-user" not in joined
    assert "am force-stop" not in joined
    assert "sysui_qs_tiles" not in joined
    assert len(runner.calls) <= 3, runner.calls
    # Explicit API flip (reassert=True) still re-applies the full policy.
    runner.calls.clear()
    assert set_device_admin_mode(runner, "RFCNB056WEV", False, reassert=True) is True
    joined = " ".join(" ".join(args) for args in runner.calls)
    assert "pm disable-user --user 0 com.android.chrome" in joined
    # device_access_mode task honours payload.reassert=false.
    runner.calls.clear()
    result = run_device_access_mode(
        adb_path="adb",
        slot_map={21: "RFCNB056WEV"},
        request=FarmTaskRequest(
            job_id="j2",
            task_type="device_access_mode",
            farm_slot_id=21,
            payload={"admin": False, "reassert": False},
        ),
        command_runner=runner,
    )
    assert result.ok is True and result.details["admin"] is False
    joined = " ".join(" ".join(args) for args in runner.calls)
    assert "pm disable-user" not in joined


def test_device_access_mode_task_sets_admin_flag():
    runner = _GuardRunner()
    runner.running = True
    result = run_device_access_mode(
        adb_path="adb",
        slot_map={21: "RFCNB056WEV"},
        request=FarmTaskRequest(
            job_id="j",
            task_type="device_access_mode",
            farm_slot_id=21,
            payload={"admin": True},
        ),
        command_runner=runner,
    )
    assert result.ok is True
    assert result.details["admin"] is True
    joined = " ".join(" ".join(args) for args in runner.calls)
    assert f"touch {ADMIN_MODE_FLAG}" in joined


def test_usb_popup_suppression_commands_and_guard_branch():
    from application.device_hardware_lockdown import (
        USB_POPUP_PACKAGES,
        apply_usb_popup_suppression,
        usb_config_has_mtp,
        usb_popup_suppression_commands,
    )

    joined = " ; ".join(usb_popup_suppression_commands())
    assert "settings put global adb_allowed_connection_time 0" in joined
    assert "settings put global usb_mass_storage_enabled 0" in joined
    assert "settings put secure usb_audio_automatic_routing_disabled 1" in joined
    assert "cmd appops set com.samsung.android.mtp SYSTEM_ALERT_WINDOW ignore" in joined
    assert "cmd appops set com.samsung.android.MtpApplication SYSTEM_ALERT_WINDOW ignore" in joined
    assert "pm disable-user --user 0 com.samsung.android.mtp" in joined
    assert "pm disable-user --user 0 com.samsung.android.MtpApplication" in joined
    # usbsettings owns the USB notification state: stopped + overlay off, not disabled.
    assert "pm disable-user --user 0 com.sec.usbsettings" not in joined
    assert "am force-stop com.sec.usbsettings" in joined
    assert "svc usb" not in joined  # runtime-only, applied separately at startup
    assert usb_config_has_mtp("mtp,conn_gadget,adb") is True
    assert usb_config_has_mtp("adb") is False

    # Guard: dismissed in BOTH modes (branch precedes the admin-mode skip) and
    # never touches the RSA UsbDebuggingActivity prompt.
    script = LOCKDOWN_GUARD_SCRIPT
    loop = script[script.index("while true; do") :]
    assert "dismiss_usb_popup" in script
    assert loop.index('dismiss_usb_popup "$pkg" "$act"') < loop.index(
        "if [ -f /data/local/tmp/mobirent_admin_mode ]; then sleep 1; continue; fi"
    )
    assert "com.samsung.android.mtp|com.samsung.android.mtpapplication|com.sec.usbsettings)" in script
    assert "*usbconfirmactivity*|*usbpermissionactivity*" in script
    assert "*usbdebuggingactivity*" not in script.lower()  # never a case pattern
    for pkg in USB_POPUP_PACKAGES:
        assert pkg.lower() in script

    # Startup applier: settings script + conditional setFunctions.
    class _UsbRunner(_GuardRunner):
        def __init__(self, config: str) -> None:
            super().__init__()
            self.config = config

        def run(self, serial: str, arguments: list[str], **kw) -> AdbCommandResult:
            if arguments[:2] == ["shell", "getprop"]:
                self.calls.append(list(arguments))
                return AdbCommandResult(stdout=self.config, stderr="")
            return super().run(serial, arguments, **kw)

    runner = _UsbRunner("mtp,conn_gadget,adb")
    assert apply_usb_popup_suppression(runner, "RFCNB056WEV", set_functions=True) is True
    flat = " || ".join(" ".join(args) for args in runner.calls)
    assert "adb_allowed_connection_time 0" in flat
    assert "svc usb setFunctions adb" in flat
    runner = _UsbRunner("adb")
    assert apply_usb_popup_suppression(runner, "18171FDF6005WG", set_functions=True) is True
    flat = " || ".join(" ".join(args) for args in runner.calls)
    assert "svc usb setFunctions" not in flat
    runner = _UsbRunner("mtp,adb")
    assert apply_usb_popup_suppression(runner, "RFCNB056WEV", set_functions=False) is True
    flat = " || ".join(" ".join(args) for args in runner.calls)
    assert "svc usb setFunctions" not in flat and "getprop" not in flat


def test_pause_renter_lockdown_restores_previous_mode():
    """Internal farm tasks pause kicks via the flag, then restore the prior state."""
    from application.device_hardware_lockdown import pause_renter_lockdown

    runner = _GuardRunner()
    runner.running = True
    runner.admin_flag = False
    with pause_renter_lockdown(runner, "RFCNB056WEV"):
        joined = " ".join(" ".join(args) for args in runner.calls)
        assert f"touch {ADMIN_MODE_FLAG}" in joined
        assert "sysui_qs_tiles" not in joined  # no policy churn for bots
        runner.calls.clear()
    joined = " ".join(" ".join(args) for args in runner.calls)
    assert f"rm -f {ADMIN_MODE_FLAG}" in joined
    assert "pm disable-user" not in joined

    runner.calls.clear()
    runner.admin_flag = True
    with pause_renter_lockdown(runner, "RFCNB056WEV"):
        runner.calls.clear()
    joined = " ".join(" ".join(args) for args in runner.calls)
    assert f"touch {ADMIN_MODE_FLAG}" in joined
    assert f"rm -f {ADMIN_MODE_FLAG}" not in joined


class _GuardRunner(AdbCommandRunner):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[list[str]] = []
        self.running = False
        self.admin_flag = False

    def run(self, serial: str, arguments: list[str], **_kwargs) -> AdbCommandResult:
        self.calls.append(list(arguments))
        joined = " ".join(arguments)
        if arguments == ["get-state"]:
            return AdbCommandResult(stdout="device", stderr="")
        if joined == f"shell if [ -f {ADMIN_MODE_FLAG} ]; then echo 1; fi":
            return AdbCommandResult(stdout="1" if self.admin_flag else "", stderr="")
        if joined == f"shell touch {ADMIN_MODE_FLAG}":
            self.admin_flag = True
        if joined == f"shell rm -f {ADMIN_MODE_FLAG}":
            self.admin_flag = False
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
        if "kill -9" in joined and "nohup sh" not in joined:
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
    assert any(args[:2] == ["shell", "ps"] for args in runner.calls)


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
            args[:2] == ["shell", "ps"] for args in runner.calls
        ):
            break
        time.sleep(0.02)
    stop.set()
    thread.join(2.0)
    joined = " ".join(" ".join(args) for args in runner.calls)
    assert LOCKDOWN_GUARD_REMOTE in joined
    assert "ps -A" in joined
    assert f"nohup sh {LOCKDOWN_GUARD_REMOTE}" in joined


S20_SERIAL = "RFCNB056WEV"
PIXEL_SERIAL = "18171FDF6005WG"
_SEC_SOUND = (
    "com.android.settings/.Settings$SecSoundSettingsActivity"
)


def _serial_online(serial: str) -> bool:
    try:
        state = AdbCommandRunner("adb", timeout_seconds=5.0).run(
            serial, ["get-state"]
        ).stdout
    except Exception:
        return False
    return str(state or "").strip() == "device"


def _s20_online() -> bool:
    return _serial_online(S20_SERIAL)


def _pixel_online() -> bool:
    return _serial_online(PIXEL_SERIAL)


def _focus_lines(stdout: str) -> str:
    lines = []
    for line in str(stdout or "").lower().splitlines():
        if "mcurrentfocus" in line or "mfocusedapp" in line or "mresumedactivity" in line:
            lines.append(line)
    return "\n".join(lines)


@pytest.mark.skipif(not _s20_online(), reason="Samsung S20 Slot 21 is not on ADB")
def test_live_s20_guard_kicks_sounds_and_vibration_and_holds_mute():
    runner = AdbCommandRunner("adb", timeout_seconds=12.0)
    try:
        runner.run(S20_SERIAL, ["shell", f"rm -f {ADMIN_MODE_FLAG}"])
    except AdbCommandError:
        pass
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
    time.sleep(1.5)
    focus = _focus_lines(
        runner.run(S20_SERIAL, ["shell", "dumpsys", "window", "displays"]).stdout
    )
    assert "secsoundsettings" not in focus, focus
    assert "soundsettings" not in focus, focus
    assert "launcher" in focus, focus
    state = runner.run(S20_SERIAL, ["shell", "cat", SCREEN_STATE_REMOTE]).stdout.strip()
    assert state.startswith("blocked_kick|blocked_sound|"), state
    top = runner.run(S20_SERIAL, ["shell", "dumpsys", "activity", "top"]).stdout.lower()
    top_head = "\n".join(top.splitlines()[:40])
    assert "secsoundsettings" not in top_head, top_head

    # On some One UI builds Settings$SecSoundSettingsActivity resolves to the
    # Settings root (homepage) instead of the sound screen. The homepage is an
    # allowed screen in customer mode, so only assert that no sound screen
    # survives -- the guard must not kick the homepage itself.
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
    assert "launcher" in focus or "com.android.settings/.settings}" in focus, focus

    runner.run(S20_SERIAL, ["shell", "settings", "put", "global", "mode_ringer", "1"])
    runner.run(S20_SERIAL, ["shell", "settings", "put", "system", "vibrate_on", "1"])
    # Audio drift is re-pinned every 10 guard ticks (~4-5s); screen kicks stay
    # on the 0.4s hot path. Poll up to 7s.
    deadline = time.monotonic() + 7.0
    ringer = vibrate = "?"
    while time.monotonic() < deadline:
        ringer = runner.run(
            S20_SERIAL, ["shell", "settings", "get", "global", "mode_ringer"]
        ).stdout.strip()
        vibrate = runner.run(
            S20_SERIAL, ["shell", "settings", "get", "system", "vibrate_on"]
        ).stdout.strip()
        if ringer == "0" and vibrate == "0":
            break
        time.sleep(0.5)
    assert ringer == "0", ringer
    assert vibrate == "0", vibrate

    runner.run(S20_SERIAL, ["shell", "am", "start", "-n", "com.android.settings/.Settings"])
    time.sleep(1.8)
    settings_focus = _focus_lines(
        runner.run(S20_SERIAL, ["shell", "dumpsys", "window", "displays"]).stdout
    )
    assert "com.android.settings" in settings_focus, settings_focus
    assert "launcher" not in settings_focus, settings_focus
    assert "secsoundsettings" not in settings_focus, settings_focus
    assert "sounds and vibration" not in settings_focus, settings_focus


def _assert_guard_running(runner: AdbCommandRunner, serial: str) -> None:
    try:
        runner.run(serial, ["shell", f"rm -f {ADMIN_MODE_FLAG}"])
    except AdbCommandError:
        pass
    assert ensure_on_device_lockdown_guard(runner, serial, redeploy=True) is True
    try:
        runner.run(
            serial,
            [
                "shell",
                "input keyevent KEYCODE_WAKEUP; true; "
                "wm dismiss-keyguard; true; "
                "input keyevent 82; true",
            ],
        )
    except AdbCommandError:
        pass
    time.sleep(1.0)
    try:
        pids = runner.run(serial, ["shell", "pgrep", "-f", "mobirent_lockdown_guard.sh"]).stdout
    except AdbCommandError:
        pids = ""
    assert any(token.isdigit() for token in pids.replace("\n", " ").split()), pids


def _assert_kicked_home(runner: AdbCommandRunner, serial: str) -> str:
    time.sleep(3.0)
    focus = _focus_lines(
        runner.run(serial, ["shell", "dumpsys", "window", "displays"]).stdout
    )
    assert "launcher" in focus, focus
    return focus


@pytest.mark.skipif(not _s20_online(), reason="Samsung S20 Slot 21 is not on ADB")
def test_live_s20_allowlist_phone_sms_and_kicks_chrome_security():
    runner = AdbCommandRunner("adb", timeout_seconds=12.0)
    _assert_guard_running(runner, S20_SERIAL)
    runner.run(S20_SERIAL, ["shell", "input", "keyevent", "KEYCODE_WAKEUP"])

    runner.run(S20_SERIAL, ["shell", "am", "start", "-a", "android.intent.action.DIAL"])
    time.sleep(0.8)
    dial = _focus_lines(
        runner.run(S20_SERIAL, ["shell", "dumpsys", "window", "displays"]).stdout
    )
    if "dialer" not in dial and "incallui" not in dial and "com.android.phone" not in dial:
        runner.run(S20_SERIAL, ["shell", "am", "start", "-a", "android.intent.action.DIAL"])
        time.sleep(2.0)
        dial = _focus_lines(
            runner.run(S20_SERIAL, ["shell", "dumpsys", "window", "displays"]).stdout
        )
    assert (
        "dialer" in dial
        or "incallui" in dial
        or "com.android.phone" in dial
        or "telephonyui" in dial
    ), dial

    runner.run(
        S20_SERIAL,
        ["shell", "am", "start", "-a", "android.intent.action.MAIN", "-c", "android.intent.category.APP_MESSAGING"],
    )
    time.sleep(1.6)
    sms = _focus_lines(
        runner.run(S20_SERIAL, ["shell", "dumpsys", "window", "displays"]).stdout
    )
    assert "messaging" in sms or "mms" in sms, sms

    try:
        runner.run(S20_SERIAL, ["shell", "pm", "enable", "--user", "0", "com.android.chrome"])
    except AdbCommandError:
        pass
    runner.run(S20_SERIAL, ["shell", "am", "start", "-n", "com.android.chrome/com.google.android.apps.chrome.Main"])
    _assert_kicked_home(runner, S20_SERIAL)

    runner.run(S20_SERIAL, ["shell", "am", "start", "-a", "android.settings.SECURITY_SETTINGS"])
    _assert_kicked_home(runner, S20_SERIAL)
    runner.run(S20_SERIAL, ["shell", "am", "start", "-a", "android.settings.NOTIFICATION_SETTINGS"])
    _assert_kicked_home(runner, S20_SERIAL)


@pytest.mark.skipif(not _pixel_online(), reason="Pixel Slot 1 is not on ADB")
def test_live_pixel_allowlist_and_kicks_chrome_play_store():
    runner = AdbCommandRunner("adb", timeout_seconds=12.0)
    _assert_guard_running(runner, PIXEL_SERIAL)

    runner.run(PIXEL_SERIAL, ["shell", "am", "start", "-a", "android.intent.action.DIAL"])
    time.sleep(1.6)
    dial = _focus_lines(runner.run(PIXEL_SERIAL, ["shell", "dumpsys", "window"]).stdout)
    assert "dialer" in dial or "incallui" in dial, dial

    try:
        runner.run(PIXEL_SERIAL, ["shell", "pm", "enable", "--user", "0", "com.android.chrome"])
    except AdbCommandError:
        pass
    runner.run(
        PIXEL_SERIAL,
        ["shell", "am", "start", "-n", "com.android.chrome/com.google.android.apps.chrome.Main"],
    )
    _assert_kicked_home(runner, PIXEL_SERIAL)
    runner.run(PIXEL_SERIAL, ["shell", "am", "start", "-a", "android.settings.SECURITY_SETTINGS"])
    _assert_kicked_home(runner, PIXEL_SERIAL)


@pytest.mark.skipif(not _s20_online(), reason="Samsung S20 Slot 21 is not on ADB")
def test_live_s20_admin_mode_allows_settings_then_client_kicks():
    from application.device_hardware_lockdown import is_device_admin_mode

    runner = AdbCommandRunner("adb", timeout_seconds=12.0)
    _assert_guard_running(runner, S20_SERIAL)
    assert set_device_admin_mode(runner, S20_SERIAL, False) is True
    time.sleep(0.3)
    runner.run(S20_SERIAL, ["shell", "am", "start", "-a", "android.settings.SOUND_SETTINGS"])
    _assert_kicked_home(runner, S20_SERIAL)
    runner.run(S20_SERIAL, ["shell", "am", "start", "-a", "android.settings.SECURITY_SETTINGS"])
    _assert_kicked_home(runner, S20_SERIAL)
    try:
        runner.run(S20_SERIAL, ["shell", "pm", "enable", "--user", "0", "com.android.chrome"])
    except AdbCommandError:
        pass
    runner.run(
        S20_SERIAL,
        ["shell", "am", "start", "-n", "com.android.chrome/com.google.android.apps.chrome.Main"],
    )
    _assert_kicked_home(runner, S20_SERIAL)

    assert set_device_admin_mode(runner, S20_SERIAL, True) is True
    time.sleep(1.2)
    assert is_device_admin_mode(runner, S20_SERIAL) is True
    try:
        pids = runner.run(
            S20_SERIAL, ["shell", "pgrep", "-f", "mobirent_lockdown_guard.sh"]
        ).stdout
    except AdbCommandError:
        pids = ""
    assert any(token.isdigit() for token in pids.replace("\n", " ").split()), pids
    runner.run(S20_SERIAL, ["shell", "am", "start", "-a", "android.settings.SOUND_SETTINGS"])
    time.sleep(1.6)
    sound = _focus_lines(
        runner.run(S20_SERIAL, ["shell", "dumpsys", "window", "displays"]).stdout
    )
    assert "settings" in sound, sound
    assert "launcher" not in sound, sound
    runner.run(S20_SERIAL, ["shell", "am", "start", "-a", "android.settings.SECURITY_SETTINGS"])
    time.sleep(1.6)
    security = _focus_lines(
        runner.run(S20_SERIAL, ["shell", "dumpsys", "window", "displays"]).stdout
    )
    assert "settings" in security or "permissioncontroller" in security or "safetycenter" in security, security
    assert "launcher" not in security, security
    try:
        runner.run(S20_SERIAL, ["shell", "pm", "enable", "--user", "0", "com.android.chrome"])
    except AdbCommandError:
        pass
    runner.run(
        S20_SERIAL,
        ["shell", "am", "start", "-n", "com.android.chrome/com.google.android.apps.chrome.Main"],
    )
    time.sleep(1.6)
    chrome = _focus_lines(
        runner.run(S20_SERIAL, ["shell", "dumpsys", "window", "displays"]).stdout
    )
    assert "chrome" in chrome, chrome
    assert "launcher" not in chrome, chrome
    runner.run(S20_SERIAL, ["shell", "input", "keyevent", "3"])
    assert set_device_admin_mode(runner, S20_SERIAL, False) is True
