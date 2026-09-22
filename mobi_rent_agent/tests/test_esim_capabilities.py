from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.esim_capabilities import (
    AndroidAuthorizationSnapshot,
    EsimProvisioningCapabilities,
    derive_esim_capabilities,
)
from domain.models import ActivationJob
from domain.ports import ProviderCapabilities


def test_stock_device_is_never_unattended():
    caps = derive_esim_capabilities(AndroidAuthorizationSnapshot(euicc_enabled=True))
    assert caps.provider_id == "human"
    assert caps.unattended is False
    assert caps.can_download is False
    assert caps.can_switch is False
    assert caps.can_delete is False
    assert caps.requires_user_consent is True
    assert caps.authorization_source == "none"
    assert "WRITE_EMBEDDED_SUBSCRIPTIONS" in caps.reason


def test_real_esim_flag_is_not_authorization():
    caps = derive_esim_capabilities(
        AndroidAuthorizationSnapshot(euicc_enabled=True, real_esim_flag=True)
    )
    assert caps.unattended is False
    assert caps.can_download is False
    assert caps.authorization_source == "none"
    assert "not authorization" in caps.reason


def test_carrier_privilege_absent_is_not_enough_with_euicc_only():
    caps = derive_esim_capabilities(
        AndroidAuthorizationSnapshot(euicc_enabled=True, has_carrier_privileges=False)
    )
    assert caps.authorization_source == "none"
    assert "carrier privileges" in caps.reason


def test_managed_permission_without_owner_is_not_authority():
    caps = derive_esim_capabilities(
        AndroidAuthorizationSnapshot(
            euicc_enabled=True,
            has_managed_subscriptions_permission=True,
            device_owner=False,
            profile_owner=False,
        )
    )
    assert caps.unattended is False
    assert caps.authorization_source == "none"
    assert "Device Owner" in caps.reason


def test_write_embedded_with_euicc_is_legitimate():
    caps = derive_esim_capabilities(
        AndroidAuthorizationSnapshot(euicc_enabled=True, has_write_embedded=True)
    )
    assert caps.provider_id == "authorized"
    assert caps.unattended is True
    assert caps.can_download is True
    assert caps.can_switch is True
    assert caps.can_delete is False
    assert caps.authorization_source == "write_embedded"


def test_carrier_with_euicc_is_legitimate():
    caps = derive_esim_capabilities(
        AndroidAuthorizationSnapshot(euicc_enabled=True, has_carrier_privileges=True)
    )
    assert caps.authorization_source == "carrier"
    assert caps.can_download is True
    assert caps.can_delete is False


def test_device_owner_without_org_owned_cannot_switch():
    caps = derive_esim_capabilities(
        AndroidAuthorizationSnapshot(euicc_enabled=True, device_owner=True)
    )
    assert caps.authorization_source == "device_owner"
    assert caps.can_download is True
    assert caps.can_switch is False
    assert caps.can_delete is False


def test_can_delete_cannot_be_true():
    with pytest.raises(ValueError, match="can_delete"):
        EsimProvisioningCapabilities(
            provider_id="authorized",
            unattended=True,
            can_download=True,
            can_switch=False,
            can_delete=True,
            requires_user_consent=False,
            authorization_source="carrier",
            reason="x",
        )
    with pytest.raises(ValueError, match="can_delete"):
        ProviderCapabilities(
            unattended=False,
            can_download=False,
            can_switch=False,
            can_delete=True,
            requires_user_consent=True,
            provider_id="human",
        )


def test_unattended_requires_legitimate_source():
    with pytest.raises(ValueError, match="authorization_source"):
        EsimProvisioningCapabilities(
            provider_id="authorized",
            unattended=True,
            can_download=True,
            can_switch=False,
            can_delete=False,
            requires_user_consent=False,
            authorization_source="none",
            reason="x",
        )


def test_switch_after_download_defaults_false():
    job = ActivationJob("job-1", 1, "LPA:1$server$secret")
    assert job.switch_after_download is False
