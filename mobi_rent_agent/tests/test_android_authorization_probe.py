from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infrastructure.android_authorization_probe import (
    parse_companion_privileged,
    parse_device_owner,
    parse_device_owner_package,
    parse_device_policy_role,
    parse_euicc_enabled_from_econtroller,
    parse_euicc_feature,
    parse_lpa_package,
    parse_organization_owned,
    parse_permission_granted,
    parse_profile_owner,
    snapshot_from_dumps,
)

STOCK_DEVICE_POLICY = """
Current Device Policy Manager state:
  Device Owner:
    none
  Profile Owner (User 0):
    none
  mDeviceOwner=null
"""

OWNED_DEVICE_POLICY = """
Current Device Policy Manager state:
  Device Owner:
    admin=ComponentInfo{com.example.mdm/com.example.mdm.AdminReceiver}
    package=com.example.mdm
"""

STOCK_PACKAGE = """
Package [com.mobirent.companion]
  pkgFlags=[ DEBUGGABLE HAS_CODE ALLOW_CLEAR_USER_DATA ]
  flags=[ DEBUGGABLE HAS_CODE ]
  requested permissions:
    android.permission.INTERNET
    android.permission.READ_PHONE_STATE
  install permissions:
    android.permission.INTERNET: granted=true
  runtime permissions:
"""

PRIVILEGED_PACKAGE = """
Package [com.mobirent.companion]
  flags=[ SYSTEM PRIVILEGED HAS_CODE ]
  grantedPermissions:
    android.permission.WRITE_EMBEDDED_SUBSCRIPTIONS
    android.permission.MANAGE_DEVICE_POLICY_MANAGED_SUBSCRIPTIONS
"""


def test_stock_policy_has_no_owners():
    assert parse_device_owner(STOCK_DEVICE_POLICY) is False
    assert parse_profile_owner(STOCK_DEVICE_POLICY) is False


def test_owned_policy_detects_device_owner():
    assert parse_device_owner(OWNED_DEVICE_POLICY) is True
    assert parse_device_owner_package(OWNED_DEVICE_POLICY) == "com.example.mdm"


def test_stock_policy_has_no_owner_package():
    assert parse_device_owner_package(STOCK_DEVICE_POLICY) is None


def test_stock_companion_is_not_privileged_and_lacks_write_embedded():
    assert parse_companion_privileged(STOCK_PACKAGE) is False
    assert parse_permission_granted(STOCK_PACKAGE, "android.permission.WRITE_EMBEDDED_SUBSCRIPTIONS") is False
    assert parse_permission_granted(
        STOCK_PACKAGE, "android.permission.MANAGE_DEVICE_POLICY_MANAGED_SUBSCRIPTIONS"
    ) is False


def test_privileged_package_grants_are_detected():
    assert parse_companion_privileged(PRIVILEGED_PACKAGE) is True
    assert parse_permission_granted(
        PRIVILEGED_PACKAGE, "android.permission.WRITE_EMBEDDED_SUBSCRIPTIONS"
    ) is True


def test_empty_role_holders_are_not_device_policy_management():
    dump = """
Role: android.app.role.DEVICE_POLICY_MANAGEMENT
  holders: []
"""
    assert parse_device_policy_role(dump) is False


def test_role_with_holder_is_detected():
    dump = """
Role: android.app.role.SYSTEM_DEVICE_POLICY_MANAGER
  holders: [com.example.mdm]
"""
    assert parse_device_policy_role(dump) is True


def test_stock_snapshot_from_companion_status():
    snapshot = snapshot_from_dumps(
        device_policy_dump=STOCK_DEVICE_POLICY,
        package_dump=STOCK_PACKAGE,
        role_dump="Role: android.app.role.DEVICE_POLICY_MANAGEMENT\n  holders: []\n",
        feature_output="true",
        econtroller_dump="EuiccServiceImpl com.android.euicc.service.EuiccServiceImpl",
        companion_status={
            "euicc_enabled": True,
            "can_silent_install": False,
            "real_esim_enabled": False,
            "has_write_embedded_subscriptions": False,
            "has_carrier_privileges": False,
        },
        real_esim_flag=True,
    )
    assert parse_euicc_feature("true") is True
    assert parse_lpa_package("com.android.euicc.service.EuiccServiceImpl") == "com.android.euicc.service.EuiccServiceImpl"
    assert snapshot.euicc_enabled is True
    assert snapshot.device_owner is False
    assert snapshot.profile_owner is False
    assert snapshot.has_write_embedded is False
    assert snapshot.has_carrier_privileges is False
    assert snapshot.companion_privileged is False
    assert snapshot.real_esim_flag is True


def test_econtroller_euiccmanager_is_enabled_line():
    assert parse_euicc_enabled_from_econtroller("EuiccManager is enabled: true") is True
    assert parse_euicc_enabled_from_econtroller("EuiccManager is enabled: false") is False


def test_organization_owned_flag_is_parsed():
    assert parse_organization_owned("isOrganizationOwnedDevice=true") is True
    assert parse_organization_owned("isOrganizationOwnedDevice=false") is False
