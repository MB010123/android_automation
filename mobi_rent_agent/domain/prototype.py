"""Isolated prototype environment types. No HTTP, ADB, or production maps."""
from __future__ import annotations

from enum import Enum


class PrototypeMode(str, Enum):
    AUDIT = "AUDIT"
    DRY_RUN = "DRY_RUN"
    REAL_TEST = "REAL_TEST"
    PRODUCTION_DISABLED = "PRODUCTION_DISABLED"


class PrototypeOperationType(str, Enum):
    AUDIT = "audit"
    DRY_RUN = "dry_run"
    DEVICE_STATUS = "device_status"
    ESIM_STATUS = "esim_status"
    VOIDFIX_STATUS = "voidfix_status"
    SEND_TEST_SMS = "send_test_sms"
    REPORT = "report"


DPC_PACKAGE = "com.mobirent.companion"
DPC_ADMIN_RECEIVER = "com.mobirent.companion.DeviceAdminReceiver"
PROVISIONING_MODE_ACTION = "android.app.action.GET_PROVISIONING_MODE"
POLICY_COMPLIANCE_ACTION = "android.app.action.ADMIN_POLICY_COMPLIANCE"

PLACEHOLDER_PREFIX = "REPLACE_WITH_"
PROTOTYPE_DEVICE_ID = "prototype-device-1"
PROTOTYPE_VOIDFIX_DEVICE_ID = 1385


def redact_msisdn(number: str) -> str:
    digits = "".join(ch for ch in number if ch.isdigit())
    if len(digits) < 4:
        return "+XXX****"
    return f"+XXX****{digits[-4:]}"


def is_placeholder(value: str | None) -> bool:
    if value is None:
        return True
    stripped = value.strip()
    return not stripped or stripped.upper().startswith(PLACEHOLDER_PREFIX)
