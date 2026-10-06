"""Narrow eSIM/VoidFix activity whitelist."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.setup_activity_guard import (
    PHASE_ESIM,
    PHASE_VOIDFIX,
    decide_setup_guard,
    parse_foreground_activity,
    parse_sms_role_holders,
)


DUMP = """
  mResumedActivity: ActivityRecord{abc u0 com.android.settings/.network.telephony.MobileNetworkActivity t10}
"""


def test_parse_resumed_activity():
    assert (
        parse_foreground_activity(DUMP)
        == "com.android.settings/.network.telephony.MobileNetworkActivity"
    )
    assert parse_foreground_activity("") is None


def test_esim_allows_mobile_network_not_generic_settings_or_launcher():
    ok = decide_setup_guard(
        phase=PHASE_ESIM,
        activity="com.android.settings/.network.telephony.MobileNetworkActivity",
    )
    assert ok.allowed is True
    generic = decide_setup_guard(phase=PHASE_ESIM, activity="com.android.settings/.Settings")
    assert generic.allowed is False
    launcher = decide_setup_guard(
        phase=PHASE_ESIM,
        activity="com.google.android.apps.nexuslauncher/.NexusLauncherActivity",
    )
    assert launcher.allowed is False
    unknown = decide_setup_guard(phase=PHASE_ESIM, activity=None)
    assert unknown.allowed is False and unknown.reason == "activity_unknown"


def test_voidfix_role_dialog_and_package():
    role = decide_setup_guard(
        phase=PHASE_VOIDFIX,
        activity="com.android.permissioncontroller/.role.ui.RequestRoleActivity",
        voidfix_package="com.voidfix.app",
    )
    assert role.allowed is True
    app = decide_setup_guard(
        phase=PHASE_VOIDFIX,
        activity="com.voidfix.app/.MainActivity",
        voidfix_package="com.voidfix.app",
    )
    assert app.allowed is True
    other = decide_setup_guard(
        phase=PHASE_VOIDFIX,
        activity="com.android.settings/.network.telephony.MobileNetworkActivity",
        voidfix_package="com.voidfix.app",
    )
    assert other.allowed is False


def test_parse_sms_role_holders():
    dump = """
Role: android.app.role.SMS
  Holders: [com.voidfix.app]
Role: android.app.role.DIALER
  Holders: [com.google.android.dialer]
"""
    assert parse_sms_role_holders(dump) == ("com.voidfix.app",)
