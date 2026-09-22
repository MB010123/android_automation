"""Device Owner / DPC diagnostic report. Reuses the authorization snapshot."""
from __future__ import annotations

from domain.esim_capabilities import AndroidAuthorizationSnapshot, derive_esim_capabilities
from domain.prototype import (
    DPC_ADMIN_RECEIVER,
    DPC_PACKAGE,
    POLICY_COMPLIANCE_ACTION,
    PROVISIONING_MODE_ACTION,
)
from infrastructure.android_authorization_probe import parse_device_owner_package

EXPECTED_HANDLERS = {
    "ACTION_GET_PROVISIONING_MODE": PROVISIONING_MODE_ACTION,
    "ACTION_ADMIN_POLICY_COMPLIANCE": POLICY_COMPLIANCE_ACTION,
    "DEVICE_ADMIN_ENABLED": "android.app.action.DEVICE_ADMIN_ENABLED",
}


def expected_provisioning_handlers() -> dict[str, str]:
    return dict(EXPECTED_HANDLERS)


def handlers_declared_in_manifest(manifest_text: str) -> dict[str, bool]:
    return {
        name: action in (manifest_text or "")
        for name, action in EXPECTED_HANDLERS.items()
    }


def build_dpc_report(
    snapshot: AndroidAuthorizationSnapshot,
    *,
    device_owner_package: str | None = None,
    dpc_package: str = DPC_PACKAGE,
    handlers: dict[str, bool] | None = None,
) -> dict:
    caps = derive_esim_capabilities(snapshot)
    owner_package = device_owner_package
    mismatch = bool(
        snapshot.device_owner
        and owner_package
        and owner_package != dpc_package
    )
    dpc_is_owner = bool(snapshot.device_owner and owner_package == dpc_package)
    if mismatch:
        status = "dpc_package_mismatch"
    elif caps.unattended and dpc_is_owner:
        status = "authorized"
    elif snapshot.device_owner:
        status = "device_owner_present"
    else:
        status = "requires_authorization"
    return {
        "device_owner": snapshot.device_owner,
        "device_owner_package": owner_package,
        "dpc_package": dpc_package,
        "dpc_admin_receiver": DPC_ADMIN_RECEIVER,
        "dpc_is_device_owner": dpc_is_owner,
        "profile_owner": snapshot.profile_owner,
        "adb_authorized_only": not snapshot.device_owner and not snapshot.profile_owner,
        "work_profile_only": snapshot.profile_owner and not snapshot.device_owner,
        "euicc_supported": snapshot.euicc_feature,
        "euicc_enabled": snapshot.euicc_enabled,
        "can_silent_install": caps.can_download,
        "can_silent_switch": caps.can_switch,
        "requires_user_consent": caps.requires_user_consent,
        "authorization_source": caps.authorization_source,
        "managed_subscriptions_permission": snapshot.has_managed_subscriptions_permission,
        "provisioning_handlers": handlers if handlers is not None else {
            name: True for name in EXPECTED_HANDLERS
        },
        "status": status,
        "reason": caps.reason,
    }


def owner_package_from_dump(device_policy_dump: str) -> str | None:
    return parse_device_owner_package(device_policy_dump)
