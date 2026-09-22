"""Legitimate Android eSIM authorization model.

REAL_ESIM_ENABLED is never sufficient by itself. Unattended download
requires a real Android authority: privileged permission, carrier
privileges, or a managed-device role that Android documents for
EuiccManager.
"""
from __future__ import annotations

from dataclasses import dataclass

from domain.ports import ProviderCapabilities

LEGITIMATE_AUTHORIZATION_SOURCES = frozenset(
    {
        "write_embedded",
        "carrier",
        "device_owner",
        "profile_owner",
        "managed_subscriptions",
    }
)


@dataclass(frozen=True)
class AndroidAuthorizationSnapshot:
    """Read-only observation of on-device authority. No secrets."""

    euicc_feature: bool = False
    euicc_enabled: bool = False
    lpa_package: str | None = None
    companion_privileged: bool = False
    has_write_embedded: bool = False
    has_carrier_privileges: bool = False
    device_owner: bool = False
    profile_owner: bool = False
    has_managed_subscriptions_permission: bool = False
    organization_owned: bool = False
    real_esim_flag: bool = False


@dataclass(frozen=True)
class EsimProvisioningCapabilities:
    provider_id: str
    unattended: bool
    can_download: bool
    can_switch: bool
    can_delete: bool
    requires_user_consent: bool
    authorization_source: str
    reason: str

    def __post_init__(self) -> None:
        if self.can_delete:
            raise ValueError("can_delete must remain false")
        if self.unattended and self.authorization_source not in LEGITIMATE_AUTHORIZATION_SOURCES:
            raise ValueError("unattended requires a legitimate authorization_source")
        if self.can_download and not self.unattended:
            raise ValueError("can_download requires unattended=true")

    def to_provider_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            unattended=self.unattended,
            can_download=self.can_download,
            can_switch=self.can_switch,
            can_delete=False,
            requires_user_consent=self.requires_user_consent,
            provider_id=self.provider_id,
            authorization_source=self.authorization_source,
            reason=self.reason,
        )


def derive_esim_capabilities(snapshot: AndroidAuthorizationSnapshot) -> EsimProvisioningCapabilities:
    """Pure gate. real_esim_flag cannot create unattended authority."""
    source = "none"
    if snapshot.has_write_embedded:
        source = "write_embedded"
    elif snapshot.has_carrier_privileges:
        source = "carrier"
    elif snapshot.device_owner:
        source = "device_owner"
    elif snapshot.profile_owner:
        source = "profile_owner"
    # MANAGE_DEVICE_POLICY_MANAGED_SUBSCRIPTIONS alone is not authority.
    # A Device Owner or Profile Owner is required for the managed path.

    authorized = source in LEGITIMATE_AUTHORIZATION_SOURCES and snapshot.euicc_enabled
    if not authorized:
        missing = []
        if not snapshot.euicc_enabled:
            missing.append("EuiccManager not enabled")
        if not snapshot.has_write_embedded:
            missing.append("WRITE_EMBEDDED_SUBSCRIPTIONS")
        if not snapshot.has_carrier_privileges:
            missing.append("carrier privileges")
        if not snapshot.device_owner:
            missing.append("Device Owner")
        if not snapshot.profile_owner:
            missing.append("Profile Owner")
        if not snapshot.has_managed_subscriptions_permission:
            missing.append("MANAGE_DEVICE_POLICY_MANAGED_SUBSCRIPTIONS")
        extra = ""
        if snapshot.real_esim_flag:
            extra = " REAL_ESIM_ENABLED=true is not authorization."
        return EsimProvisioningCapabilities(
            provider_id="human",
            unattended=False,
            can_download=False,
            can_switch=False,
            can_delete=False,
            requires_user_consent=True,
            authorization_source="none",
            reason="no legitimate Android eSIM authority: " + ", ".join(missing) + "." + extra,
        )

    can_switch = source in {"write_embedded", "carrier"}
    if source in {"device_owner", "profile_owner", "managed_subscriptions"}:
        can_switch = snapshot.organization_owned
    return EsimProvisioningCapabilities(
        provider_id="authorized",
        unattended=True,
        can_download=True,
        can_switch=can_switch,
        can_delete=False,
        requires_user_consent=False,
        authorization_source=source,
        reason=f"legitimate authorization_source={source}",
    )
