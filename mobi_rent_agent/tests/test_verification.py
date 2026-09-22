from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.models import ProvisioningResult
from domain.provisioning_state import ActivationVerdict
from domain.verification import (
    ConnectivityLayer,
    FourLayerVerification,
    SettingsLpaLayer,
    SubscriptionLayer,
    TelephonyLayer,
    evaluate_activation,
)


def _snapshot(
    *,
    settings_visible: bool | None = True,
    euicc_enabled: bool | None = True,
    subscription_present: bool | None = False,
    subscription_embedded: bool | None = False,
    default_data_sub_id: int | None = -1,
    data_registered: bool | None = False,
    emergency_only: bool | None = True,
    wifi_enabled: bool | None = False,
    cellular_transport: bool | None = False,
    cellular_ip: bool | None = False,
    default_route_cellular: bool | None = False,
    internet_reachable: bool | None = False,
    internet_proves_cellular: bool | None = False,
    observation_complete: bool = True,
    explicit_failure: bool = False,
    failure_reason: str | None = None,
) -> FourLayerVerification:
    return FourLayerVerification(
        settings_lpa=SettingsLpaLayer(
            euicc_supported=True,
            euicc_enabled=euicc_enabled,
            lpa_package="com.google.android.euicc",
            lpa_last_profile_list_result=131082,
            lpa_last_card_id=-2,
            lpa_last_slot_id=-1,
            settings_profile_visible=settings_visible,
            settings_carrier_name="US Mobile" if settings_visible else None,
            settings_attested_at="2026-09-06T20:00:00Z" if settings_visible else None,
            settings_attested_by="operator" if settings_visible else None,
        ),
        subscription=SubscriptionLayer(
            subscription_present=subscription_present,
            subscription_embedded=subscription_embedded,
            subscription_id=12 if subscription_embedded else None,
            default_data_sub_id=default_data_sub_id,
            sim_slot_index=1 if subscription_embedded else -1,
            port_index=0 if subscription_embedded else -1,
            card_id=1 if subscription_embedded else -2,
            carrier_name_framework="US Mobile" if subscription_embedded else None,
            embedded_list_empty=not subscription_embedded,
        ),
        telephony=TelephonyLayer(
            gsm_sim_state="READY" if data_registered else "ABSENT",
            sim_state_slot0="ABSENT",
            sim_state_slot1="READY" if data_registered else "UNKNOWN",
            voice_registered=data_registered,
            data_registered=data_registered,
            emergency_only=emergency_only,
            operator_numeric="310260" if data_registered else "",
            radio_access="LTE",
            data_connection_state=2 if data_registered else -1,
        ),
        connectivity=ConnectivityLayer(
            wifi_enabled=wifi_enabled,
            airplane_mode=False,
            mobile_data_setting=True,
            cellular_transport_available=cellular_transport,
            cellular_ip_present=cellular_ip,
            default_route_cellular=default_route_cellular,
            internet_reachable=internet_reachable,
            internet_proves_cellular=internet_proves_cellular,
            active_default_network="CELLULAR" if cellular_transport else "none",
        ),
        observation_complete=observation_complete,
        explicit_failure=explicit_failure,
        failure_reason=failure_reason,
    )


def test_slot1_partial_settings_visible_framework_missing():
    verdict = evaluate_activation(_snapshot())
    assert verdict is ActivationVerdict.ACTIVATION_PARTIAL
    result = ProvisioningResult.from_verdict("job-1", 1, verdict, error="partial")
    assert result.success is False
    assert result.verdict is ActivationVerdict.ACTIVATION_PARTIAL


def test_confirmed_requires_all_layers():
    verdict = evaluate_activation(
        _snapshot(
            subscription_present=True,
            subscription_embedded=True,
            default_data_sub_id=12,
            data_registered=True,
            emergency_only=False,
            cellular_transport=True,
            cellular_ip=True,
            default_route_cellular=True,
        )
    )
    assert verdict is ActivationVerdict.ACTIVATION_CONFIRMED
    result = ProvisioningResult.from_verdict("job-1", 1, verdict)
    assert result.success is True


def test_failed_when_complete_and_no_settings_evidence():
    verdict = evaluate_activation(
        _snapshot(
            settings_visible=False,
            euicc_enabled=False,
            subscription_present=False,
            subscription_embedded=False,
            data_registered=False,
            emergency_only=True,
            wifi_enabled=False,
            cellular_transport=False,
            internet_reachable=False,
        )
    )
    assert verdict is ActivationVerdict.ACTIVATION_FAILED


def test_explicit_failure():
    verdict = evaluate_activation(
        _snapshot(explicit_failure=True, failure_reason="conflicting profile")
    )
    assert verdict is ActivationVerdict.ACTIVATION_FAILED
    result = ProvisioningResult.from_verdict("job-1", 1, verdict, error="conflicting profile")
    assert result.success is False


def test_unknown_when_observation_incomplete():
    verdict = evaluate_activation(_snapshot(settings_visible=None, observation_complete=False))
    assert verdict is ActivationVerdict.VERIFICATION_UNKNOWN
    assert verdict.success is False


def test_wifi_internet_is_not_cellular_proof():
    verdict = evaluate_activation(
        _snapshot(
            settings_visible=None,
            euicc_enabled=True,
            subscription_present=True,
            subscription_embedded=True,
            default_data_sub_id=12,
            data_registered=True,
            emergency_only=False,
            wifi_enabled=True,
            cellular_transport=False,
            cellular_ip=False,
            default_route_cellular=False,
            internet_reachable=True,
            internet_proves_cellular=True,
        )
    )
    assert verdict is not ActivationVerdict.ACTIVATION_CONFIRMED


def test_wifi_with_settings_evidence_is_partial_not_confirmed():
    verdict = evaluate_activation(
        _snapshot(
            wifi_enabled=True,
            internet_reachable=True,
            internet_proves_cellular=True,
            cellular_transport=False,
        )
    )
    assert verdict is ActivationVerdict.ACTIVATION_PARTIAL
    assert verdict.success is False


def test_success_true_rejected_for_partial_result():
    with pytest.raises(ValueError, match="ACTIVATION_CONFIRMED"):
        ProvisioningResult(
            success=True,
            job_id="job-1",
            slot_id=1,
            verdict=ActivationVerdict.ACTIVATION_PARTIAL,
        )


def test_success_true_without_verdict_is_rejected():
    with pytest.raises(ValueError, match="ACTIVATION_CONFIRMED"):
        ProvisioningResult(success=True, job_id="job-1", slot_id=1)


def test_log_dict_has_no_secrets():
    payload = _snapshot().to_log_dict()
    dumped = str(payload)
    assert "LPA:" not in dumped
    assert "imei" not in dumped.lower()
    assert "eid" not in dumped.lower()
    assert "msisdn" not in dumped.lower()
    assert "activation_code" not in dumped
