"""Classify Pixel Settings / eSIM / VoidFix surfaces for customer rentals."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.session_restriction_policy import (
    CAMERA_PACKAGES,
    COMPANION_PACKAGE,
    DEST_ADD_ESIM,
    DEST_HOME,
    GADS_SETTINGS_PACKAGE,
    KIND_BLOCKED_APP,
    KIND_ESIM_NESTED,
    KIND_ESIM_ROOT,
    KIND_LAUNCHER,
    KIND_OTHER,
    KIND_SETTINGS,
    KIND_SHADE,
    REASON_ESIM_BACK_HOME,
    REASON_QR_NAVIGATE,
    REASON_SENSITIVE_BLOCKED,
    REASON_SETTINGS_REDIRECT,
    REASON_SHADE_BLOCKED,
    is_shade_pull_swipe,
    is_sound_settings_activity,
    add_esim_public_body,
    classify_foreground_activity,
    decide_restriction,
    operator_ui_packages,
    voidfix_packages,
)


def test_known_voidfix_includes_slot11_package():
    packages = voidfix_packages(None)
    assert "org.voidfix.smsgateway" in packages
    assert "com.voidfix.app" in packages
    assert "org.voidfix.smsgateway" in voidfix_packages("org.voidfix.smsgateway")


def test_classify_esim_settings_voidfix_and_apps():
    assert (
        classify_foreground_activity(
            "com.android.settings/.network.telephony.MobileNetworkActivity"
        ).kind
        == KIND_ESIM_ROOT
    )
    assert (
        classify_foreground_activity("com.google.android.euicc/.ui.EuiccProvisioningActivity").kind
        == KIND_ESIM_NESTED
    )
    assert (
        classify_foreground_activity(
            "com.samsung.android.app.telephonyui/.netsettings.ui.simcardmanager.SimCardMgrActivity"
        ).kind
        == KIND_ESIM_NESTED
    )
    assert (
        classify_foreground_activity(
            "com.samsung.android.euicc/.ui.EuiccProvisioningActivity"
        ).kind
        == KIND_ESIM_NESTED
    )
    assert (
        classify_foreground_activity("com.sec.android.gallery3d/.GalleryActivity").kind
        == KIND_ESIM_NESTED
    )
    assert (
        classify_foreground_activity(
            "com.google.android.documentsui/.picker.PickActivity"
        ).kind
        == KIND_ESIM_NESTED
    )
    assert (
        classify_foreground_activity(
            "com.samsung.android.biometrics.app.setting/.BiometricsActivity"
        ).kind
        == KIND_ESIM_NESTED
    )
    assert classify_foreground_activity(
        "com.samsung.android.game.gamehome/.MainActivity"
    ).kind == KIND_BLOCKED_APP
    assert classify_foreground_activity("com.android.settings/.Settings").kind == KIND_SETTINGS
    assert (
        classify_foreground_activity("com.android.settings/com.android.settings.Settings").kind
        == KIND_SETTINGS
    )
    assert (
        classify_foreground_activity(
            "com.android.settings/.homepage.SettingsHomepageActivity"
        ).kind
        == KIND_SETTINGS
    )
    assert classify_foreground_activity("com.android.settings/.spa.SpaActivity").kind == KIND_ESIM_NESTED
    assert (
        classify_foreground_activity(
            "com.android.settings/.Settings$NetworkDashboardActivity"
        ).kind
        == KIND_ESIM_NESTED
    )
    assert (
        classify_foreground_activity(
            "com.android.settings/.Settings$MobileNetworkListActivity"
        ).kind
        == KIND_ESIM_ROOT
    )
    assert (
        classify_foreground_activity("com.android.settings/.Settings$SecurityDashboardActivity").kind
        == KIND_SETTINGS
    )
    assert (
        classify_foreground_activity(
            "com.android.settings/.Settings$DevelopmentSettingsDashboardActivity"
        ).kind
        == KIND_SETTINGS
    )
    assert (
        classify_foreground_activity("com.android.settings/.Settings$AccountDashboardActivity").kind
        == KIND_SETTINGS
    )
    assert (
        classify_foreground_activity("com.android.settings/.wifi.WifiSettings").kind == KIND_SETTINGS
    )
    assert (
        classify_foreground_activity("com.android.settings/.applications.ManageApplications").kind
        == KIND_SETTINGS
    )
    assert (
        classify_foreground_activity("com.android.settings/.system.SystemDashboardFragment").kind
        == KIND_SETTINGS
    )
    assert (
        classify_foreground_activity("com.google.android.apps.nexuslauncher/.NexusLauncherActivity").kind
        == KIND_LAUNCHER
    )
    assert classify_foreground_activity("org.voidfix.smsgateway/.MainActivity").kind == KIND_BLOCKED_APP
    assert (
        classify_foreground_activity(
            "com.voidfix.app/.MainActivity", voidfix_package="com.voidfix.app"
        ).kind
        == KIND_BLOCKED_APP
    )
    assert (
        classify_foreground_activity("com.android.packageinstaller/.InstallStart").kind
        == KIND_BLOCKED_APP
    )
    assert (
        classify_foreground_activity(f"{GADS_SETTINGS_PACKAGE}/.MainActivity").kind
        == KIND_BLOCKED_APP
    )
    assert (
        classify_foreground_activity(f"{GADS_SETTINGS_PACKAGE}/com.shamanec.settings.MainActivity").kind
        == KIND_BLOCKED_APP
    )
    assert (
        classify_foreground_activity(f"{COMPANION_PACKAGE}/.MainActivity").kind == KIND_BLOCKED_APP
    )
    assert (
        classify_foreground_activity(
            f"{GADS_SETTINGS_PACKAGE}/com.shamanec.settings.streaming.MjpegScreenCaptureActivity"
        ).kind
        == KIND_OTHER
    )
    assert (
        classify_foreground_activity(
            f"{GADS_SETTINGS_PACKAGE}/com.shamanec.settings.audio.AudioProjectionActivity"
        ).kind
        == KIND_OTHER
    )
    assert classify_foreground_activity("com.android.chrome/.MainActivity").kind == KIND_OTHER
    assert (
        classify_foreground_activity("com.google.android.GoogleCamera/.CameraActivity").kind
        == KIND_BLOCKED_APP
    )
    assert (
        classify_foreground_activity(
            "com.google.android.GoogleCamera/.CameraActivity",
            previous_kind=KIND_LAUNCHER,
        ).kind
        == KIND_BLOCKED_APP
    )
    assert (
        classify_foreground_activity(
            "com.google.android.GoogleCamera/.CameraActivity",
            previous_kind=KIND_ESIM_ROOT,
        ).kind
        == KIND_BLOCKED_APP
    )
    assert (
        classify_foreground_activity(
            "com.google.android.GoogleCamera/.QrCodeScanActivity",
        ).kind
        == KIND_BLOCKED_APP
    )
    assert (
        classify_foreground_activity("com.android.camera2/.CameraActivity").kind
        == KIND_BLOCKED_APP
    )
    assert "com.google.android.GoogleCamera" in CAMERA_PACKAGES
    assert (
        classify_foreground_activity("com.android.systemui/.shade.NotificationShadeWindow").kind
        == KIND_SHADE
    )
    assert (
        classify_foreground_activity("com.android.systemui/.qs.QSPanel").kind == KIND_SHADE
    )
    assert is_shade_pull_swipe(y=8, y2=400, x=100, x2=100) is True
    assert is_shade_pull_swipe(y=2900, y2=2000, x=720, x2=720) is False
    assert is_shade_pull_swipe(y=8, y2=10, x=100, x2=100) is False
    assert (
        classify_foreground_activity(
            "com.android.settings/.Settings$SoundSettingsActivity"
        ).kind
        == KIND_SETTINGS
    )
    assert is_sound_settings_activity(
        "com.android.settings/.Settings$SoundSettingsActivity"
    )
    assert is_sound_settings_activity(
        "com.android.settings/.notification.SoundSettings"
    )
    assert not is_sound_settings_activity(
        "com.android.settings/.network.telephony.MobileNetworkActivity"
    )
    sound = decide_restriction(
        action="tap",
        activity="com.android.settings/.Settings$SoundSettingsActivity",
    )
    assert sound.escape_settings is True and sound.launch_esim is True


def test_operator_ui_packages_are_exact_system_identities():
    packages = operator_ui_packages()
    assert packages == frozenset({GADS_SETTINGS_PACKAGE, COMPANION_PACKAGE})
    assert GADS_SETTINGS_PACKAGE == "com.gads.settings"
    assert COMPANION_PACKAGE == "com.mobirent.companion"


def test_settings_and_back_decisions():
    settings = decide_restriction(action="settings", activity=None)
    assert settings.launch_esim is True and settings.destination == DEST_ADD_ESIM
    back_root = decide_restriction(
        action="back",
        activity="com.android.settings/.network.telephony.MobileNetworkActivity",
    )
    assert back_root.send_home is True and back_root.reason == REASON_ESIM_BACK_HOME
    back_nested = decide_restriction(
        action="back",
        activity="com.google.android.euicc/.ui.ConfirmDownloadActivity",
    )
    assert back_nested.allow_original is True and back_nested.send_home is False
    tap_settings = decide_restriction(
        action="tap", activity="com.android.settings/.Settings$SecurityDashboardActivity"
    )
    assert tap_settings.launch_esim is True and tap_settings.reason == REASON_SETTINGS_REDIRECT
    assert tap_settings.escape_settings is True
    tap_homepage = decide_restriction(action="tap", activity="com.android.settings/.Settings")
    assert tap_homepage.escape_settings is True and tap_homepage.launch_esim is True
    tap_spa = decide_restriction(action="tap", activity="com.android.settings/.spa.SpaActivity")
    assert tap_spa.send_home is False and tap_spa.escape_settings is False
    tap_network = decide_restriction(
        action="tap", activity="com.android.settings/.Settings$NetworkDashboardActivity"
    )
    assert tap_network.send_home is False and tap_network.escape_settings is False
    settings_from_homepage = decide_restriction(
        action="settings", activity="com.android.settings/.Settings"
    )
    assert settings_from_homepage.escape_settings is True
    settings_from_home = decide_restriction(
        action="settings",
        activity="com.google.android.apps.nexuslauncher/.NexusLauncherActivity",
    )
    assert settings_from_home.launch_esim is True and settings_from_home.escape_settings is False
    tap_voidfix = decide_restriction(
        action="tap", activity="org.voidfix.smsgateway/.SmsGatewayActivity"
    )
    assert tap_voidfix.send_home is True and tap_voidfix.reason == REASON_SENSITIVE_BLOCKED
    tap_gads_settings = decide_restriction(
        action="tap", activity=f"{GADS_SETTINGS_PACKAGE}/.MainActivity"
    )
    assert tap_gads_settings.send_home is True and tap_gads_settings.reason == REASON_SENSITIVE_BLOCKED
    tap_companion = decide_restriction(action="tap", activity=f"{COMPANION_PACKAGE}/.MainActivity")
    assert tap_companion.send_home is True and tap_companion.reason == REASON_SENSITIVE_BLOCKED
    tap_camera = decide_restriction(
        action="tap", activity="com.google.android.GoogleCamera/.CameraActivity"
    )
    assert tap_camera.send_home is True and tap_camera.reason == REASON_SENSITIVE_BLOCKED
    tap_esim_camera = decide_restriction(
        action="tap",
        activity="com.google.android.GoogleCamera/.CameraActivity",
        previous_kind=KIND_ESIM_ROOT,
    )
    assert tap_esim_camera.send_home is True and tap_esim_camera.reason == REASON_SENSITIVE_BLOCKED
    tap_esim_qr = decide_restriction(
        action="tap",
        activity="com.google.android.euicc/.ui.QrCodeScanActivity",
    )
    assert tap_esim_qr.send_home is False and tap_esim_qr.destination is None
    tap_scan_image = decide_restriction(
        action="tap",
        activity="com.google.android.photospicker/.PhotoPickerActivity",
        previous_kind=KIND_ESIM_NESTED,
    )
    assert tap_scan_image.send_home is False and tap_scan_image.destination is None
    tap_gads_stream = decide_restriction(
        action="tap",
        activity=f"{GADS_SETTINGS_PACKAGE}/com.shamanec.settings.streaming.MjpegScreenCaptureActivity",
    )
    assert tap_gads_stream.send_home is False and tap_gads_stream.destination is None
    shade = decide_restriction(action="notification_shade", activity=None)
    assert shade.send_home is True and shade.reason == REASON_SHADE_BLOCKED
    qs = decide_restriction(action="quick_settings", activity=None)
    assert qs.send_home is True and qs.reason == REASON_SHADE_BLOCKED
    top_swipe = decide_restriction(
        action="swipe", activity=None, x=100, y=8, x2=100, y2=400
    )
    assert top_swipe.allow_original is False and top_swipe.reason == REASON_SHADE_BLOCKED
    assert top_swipe.send_home is False
    bottom = decide_restriction(
        action="swipe",
        activity="com.android.chrome/.MainActivity",
        x=720,
        y=2900,
        x2=720,
        y2=2000,
    )
    assert bottom.allow_original is True and bottom.send_home is False
    visible_shade = decide_restriction(
        action="tap",
        activity="com.android.systemui/.shade.NotificationShadeWindow",
    )
    assert visible_shade.send_home is True and visible_shade.reason == REASON_SHADE_BLOCKED
    tap_home = decide_restriction(
        action="tap",
        activity="com.google.android.apps.nexuslauncher/.NexusLauncherActivity",
    )
    assert tap_home.destination is None
    inspect_fail = decide_restriction(action="tap", activity=None, inspect_failed=True)
    assert inspect_fail.destination is None
    inspect_fail_back = decide_restriction(action="back", activity=None, inspect_failed=True)
    assert inspect_fail_back.allow_original is True and inspect_fail_back.send_home is False
    tap_esim_root = decide_restriction(
        action="tap",
        activity="com.android.settings/.network.telephony.MobileNetworkActivity",
    )
    assert tap_esim_root.launch_esim is False and tap_esim_root.send_home is False
    assert tap_esim_root.escape_settings is False
    tap_samsung_sim = decide_restriction(
        action="tap",
        activity="com.samsung.android.app.telephonyui/.netsettings.ui.simcardmanager.SimCardMgrActivity",
    )
    assert tap_samsung_sim.launch_esim is False and tap_samsung_sim.send_home is False
    tap_samsung_gallery = decide_restriction(
        action="tap",
        activity="com.sec.android.gallery3d/.GalleryActivity",
    )
    assert tap_samsung_gallery.launch_esim is False and tap_samsung_gallery.send_home is False
    tap_installer = decide_restriction(
        action="tap",
        activity="com.android.packageinstaller/.InstallStart",
    )
    assert tap_installer.send_home is True and tap_installer.reason == REASON_SENSITIVE_BLOCKED
    delete_esim = decide_restriction(
        action="tap",
        activity="com.android.settings/.network.telephony.MobileNetworkActivity",
    )
    assert delete_esim.launch_esim is False and delete_esim.send_home is False
    body = add_esim_public_body(reason=REASON_QR_NAVIGATE)
    assert body == {
        "restricted_destination": DEST_ADD_ESIM,
        "restriction": REASON_QR_NAVIGATE,
    }
    assert "serial" not in body and "intent" not in body
