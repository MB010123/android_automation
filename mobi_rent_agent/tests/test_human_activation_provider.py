from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.models import ActivationJob
from domain.provisioning_state import ActivationVerdict, JobState
from domain.verification import (
    ConnectivityLayer,
    FourLayerVerification,
    SettingsLpaLayer,
    SubscriptionLayer,
    TelephonyLayer,
)
from infrastructure.human_activation_provider import HumanActivationProvider


def test_capabilities_are_human_only():
    caps = HumanActivationProvider().capabilities("SERIAL-1")
    assert caps.unattended is False
    assert caps.can_download is False
    assert caps.can_switch is False
    assert caps.can_delete is False
    assert caps.requires_user_consent is True
    assert caps.provider_id == "human"
    assert caps.authorization_source == "none"
    assert caps.reason


def test_submit_activation_never_sends_code():
    job = ActivationJob("job-1", 1, "LPA:1$server$secret")
    result = HumanActivationProvider().submit_activation("SERIAL-1", job)
    assert result.accepted is False
    assert result.activation_code_sent is False
    assert result.state is JobState.WAITING_FOR_ACTIVATION
    assert "not sent" in (result.error or "")
    assert result.__dict__.get("activation_code") is None


def test_provision_never_includes_activation_code():
    job = ActivationJob("job-1", 1, "LPA:1$server$secret")
    result = HumanActivationProvider().provision("SERIAL-1", job)
    dumped = result.to_dict()
    assert result.success is False
    assert "activation_code" not in dumped
    assert "secret" not in str(dumped)
    assert "LPA:" not in str(dumped)


def test_activate_profile_is_refused():
    job = ActivationJob("job-1", 1, "LPA:1$server$secret")
    result = HumanActivationProvider().activate_profile("SERIAL-1", job, 12)
    assert result.accepted is False
    assert result.activation_code_sent is False


def test_verify_profile_partial_is_not_success():
    job = ActivationJob("job-1", 1, qr_url="https://example.test/qr.png")
    snapshot = FourLayerVerification(
        settings_lpa=SettingsLpaLayer(euicc_enabled=True, settings_profile_visible=True),
        subscription=SubscriptionLayer(
            subscription_present=False,
            subscription_embedded=False,
            default_data_sub_id=-1,
            embedded_list_empty=True,
        ),
        telephony=TelephonyLayer(data_registered=False, emergency_only=True),
        connectivity=ConnectivityLayer(
            wifi_enabled=True,
            cellular_transport_available=False,
            cellular_ip_present=False,
            default_route_cellular=False,
            internet_reachable=False,
        ),
    )
    result = HumanActivationProvider().verify_profile("SERIAL-1", job, snapshot)
    assert result.verdict is ActivationVerdict.ACTIVATION_PARTIAL
    assert result.success is False


def test_provider_has_no_euicc_manager():
    import infrastructure.human_activation_provider as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "downloadSubscription(" not in source
    assert "switchToSubscription(" not in source
    assert "telephony.euicc" not in source
    assert "EuiccManager." not in source
