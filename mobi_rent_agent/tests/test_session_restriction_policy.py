"""Classify Pixel Settings / eSIM / VoidFix surfaces for customer rentals."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.session_restriction_policy import (
    DEST_ADD_ESIM,
    DEST_HOME,
    KIND_BLOCKED_APP,
    KIND_ESIM_NESTED,
    KIND_ESIM_ROOT,
    KIND_LAUNCHER,
    KIND_OTHER,
    KIND_SETTINGS,
    REASON_ESIM_BACK_HOME,
    REASON_QR_NAVIGATE,
    REASON_SENSITIVE_BLOCKED,
    REASON_SETTINGS_REDIRECT,
    add_esim_public_body,
    classify_foreground_activity,
    decide_restriction,
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
    assert classify_foreground_activity("com.android.settings/.Settings").kind == KIND_SETTINGS
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
        classify_foreground_activity("com.android.settings/.Settings$NetworkDashboardActivity").kind
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
    assert classify_foreground_activity("com.android.chrome/.MainActivity").kind == KIND_OTHER


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
    tap_voidfix = decide_restriction(
        action="tap", activity="org.voidfix.smsgateway/.SmsGatewayActivity"
    )
    assert tap_voidfix.send_home is True and tap_voidfix.reason == REASON_SENSITIVE_BLOCKED
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
