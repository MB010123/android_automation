from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.esim_capabilities import AndroidAuthorizationSnapshot, derive_esim_capabilities
from domain.models import ActivationJob
from domain.provisioning_state import ActivationVerdict, JobState
from domain.slot_isolation import SlotIsolationPolicy
from domain.verification import (
    ConnectivityLayer,
    FourLayerVerification,
    SettingsLpaLayer,
    SubscriptionLayer,
    TelephonyLayer,
)
from infrastructure.android_authorization_probe import StaticAuthorizationProbe
from infrastructure.authorized_esim_provider import AuthorizedEsimProvider, select_esim_provider
from infrastructure.human_activation_provider import HumanActivationProvider

SECRET = "LPA:1$server$secret"
MANAGED_SOURCES = frozenset({"device_owner", "profile_owner", "managed_subscriptions"})


def _job(slot_id: int = 1, **kwargs) -> ActivationJob:
    return ActivationJob("job-1", slot_id, SECRET, **kwargs)


def _provider(snapshot: AndroidAuthorizationSnapshot, **kwargs) -> AuthorizedEsimProvider:
    return AuthorizedEsimProvider(
        StaticAuthorizationProbe(snapshot),
        SlotIsolationPolicy({1}),
        **kwargs,
    )


def _layers(
    *,
    settings_visible: bool | None = True,
    subscription_embedded: bool = False,
    data_registered: bool = False,
    cellular: bool = False,
    observation_complete: bool = True,
    explicit_failure: bool = False,
    failure_reason: str | None = None,
) -> FourLayerVerification:
    return FourLayerVerification(
        settings_lpa=SettingsLpaLayer(
            euicc_enabled=True,
            settings_profile_visible=settings_visible,
        ),
        subscription=SubscriptionLayer(
            subscription_present=subscription_embedded,
            subscription_embedded=subscription_embedded,
            default_data_sub_id=12 if subscription_embedded else -1,
            embedded_list_empty=not subscription_embedded,
        ),
        telephony=TelephonyLayer(
            data_registered=data_registered,
            emergency_only=not data_registered,
        ),
        connectivity=ConnectivityLayer(
            wifi_enabled=not cellular,
            cellular_transport_available=cellular,
            cellular_ip_present=cellular,
            default_route_cellular=cellular,
            internet_reachable=cellular,
            internet_proves_cellular=cellular,
        ),
        observation_complete=observation_complete,
        explicit_failure=explicit_failure,
        failure_reason=failure_reason,
    )


def test_no_android_authorization_provider_refuses():
    provider = _provider(AndroidAuthorizationSnapshot(euicc_enabled=True))
    caps = provider.capabilities("SERIAL-1")
    assert caps.unattended is False
    assert caps.can_download is False
    assert caps.authorization_source == "none"
    result = provider.submit_activation("SERIAL-1", _job())
    assert result.accepted is False
    assert result.activation_code_sent is False
    assert result.state is JobState.WAITING_FOR_ACTIVATION
    assert "authority" in (result.error or "")


def test_carrier_privilege_absent_provider_refuses():
    provider = _provider(
        AndroidAuthorizationSnapshot(euicc_enabled=True, has_carrier_privileges=False)
    )
    result = provider.submit_activation("SERIAL-1", _job())
    assert result.accepted is False
    assert result.activation_code_sent is False
    assert "carrier privileges" in (provider.capabilities().reason)


def test_managed_provider_refuses_without_device_or_profile_owner():
    snapshot = AndroidAuthorizationSnapshot(
        euicc_enabled=True,
        has_managed_subscriptions_permission=True,
        device_owner=False,
        profile_owner=False,
        has_carrier_privileges=True,
    )
    provider = _provider(snapshot, required_sources=MANAGED_SOURCES)
    result = provider.submit_activation("SERIAL-1", _job())
    assert result.accepted is False
    assert result.activation_code_sent is False
    assert "Device Owner or Profile Owner" in (result.error or "")


def test_can_download_false_never_sends_or_downloads():
    provider = _provider(AndroidAuthorizationSnapshot(euicc_enabled=True, real_esim_flag=True))
    assert provider.capabilities().can_download is False
    result = provider.submit_activation("SERIAL-1", _job())
    assert result.accepted is False
    assert result.activation_code_sent is False
    source = Path(
        __import__("infrastructure.authorized_esim_provider", fromlist=["authorized_esim_provider"]).__file__
    ).read_text(encoding="utf-8")
    assert "downloadSubscription" not in source
    assert "switchToSubscription" not in source
    assert "EuiccManager" not in source


def test_slot_2_is_rejected():
    provider = _provider(
        AndroidAuthorizationSnapshot(euicc_enabled=True, has_carrier_privileges=True)
    )
    result = provider.submit_activation("SERIAL-2", _job(2))
    assert result.accepted is False
    assert result.state is JobState.FAILED
    assert "allowlist" in (result.error or "")
    assert result.activation_code_sent is False


def test_slot_1_allowed_only_after_capability_gate():
    unauthorized = _provider(AndroidAuthorizationSnapshot(euicc_enabled=True))
    refused = unauthorized.submit_activation("SERIAL-1", _job())
    assert refused.accepted is False

    authorized_snapshot = AndroidAuthorizationSnapshot(
        euicc_enabled=True,
        has_carrier_privileges=True,
    )
    gated = _provider(authorized_snapshot, live_download_armed=False)
    caps = gated.esim_capabilities()
    assert caps.unattended is True
    assert caps.can_download is True
    assert caps.authorization_source == "carrier"
    still_refused = gated.submit_activation("SERIAL-1", _job())
    assert still_refused.accepted is False
    assert still_refused.activation_code_sent is False
    assert "not armed" in (still_refused.error or "")


def test_activation_code_never_appears_in_logs(caplog):
    provider = _provider(AndroidAuthorizationSnapshot(euicc_enabled=True))
    with caplog.at_level(logging.DEBUG):
        result = provider.submit_activation("SERIAL-1", _job())
        dumped = result.__dict__
    text = caplog.text + str(dumped) + str(result.error)
    assert SECRET not in text
    assert "LPA:" not in text
    assert "activation_code" not in dumped


def test_activation_code_never_persists_to_disk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    provider = _provider(
        AndroidAuthorizationSnapshot(euicc_enabled=True, has_carrier_privileges=True)
    )
    provider.submit_activation("SERIAL-1", _job())
    provider.provision("SERIAL-1", _job())
    written = [path for path in tmp_path.rglob("*") if path.is_file()]
    for path in written:
        contents = path.read_text(encoding="utf-8", errors="ignore")
        assert SECRET not in contents
        assert "LPA:" not in contents
    module = Path(
        __import__("infrastructure.authorized_esim_provider", fromlist=["x"]).__file__
    ).read_text(encoding="utf-8")
    assert "open(" not in module
    assert "write_text" not in module
    assert "Path.write" not in module


def test_can_delete_always_false_even_when_authorized():
    caps = derive_esim_capabilities(
        AndroidAuthorizationSnapshot(euicc_enabled=True, has_write_embedded=True)
    )
    assert caps.can_delete is False
    provider = _provider(AndroidAuthorizationSnapshot(euicc_enabled=True, has_write_embedded=True))
    assert provider.capabilities().can_delete is False


def test_device_owner_forwards_switch_after_download():
    from infrastructure.authorized_esim_provider import download_may_switch_after

    caps = derive_esim_capabilities(
        AndroidAuthorizationSnapshot(euicc_enabled=True, device_owner=True)
    )
    assert caps.can_switch is False
    assert download_may_switch_after(caps, True) is True
    assert download_may_switch_after(caps, False) is False

    seen: list[bool] = []

    def transport(serial, job):
        seen.append(job.switch_after_download)
        return {"success": True, "device_code": 0}

    provider = _provider(
        AndroidAuthorizationSnapshot(euicc_enabled=True, device_owner=True),
        live_download_armed=True,
        download_transport=transport,
    )
    result = provider.submit_activation("SERIAL-1", _job(switch_after_download=True))
    assert result.accepted is True
    assert seen == [True]


def test_switch_after_download_still_defaults_off_for_device_owner():
    seen: list[bool] = []

    def transport(serial, job):
        seen.append(job.switch_after_download)
        return {"success": True, "device_code": 0}

    provider = _provider(
        AndroidAuthorizationSnapshot(euicc_enabled=True, device_owner=True),
        live_download_armed=True,
        download_transport=transport,
    )
    provider.submit_activation("SERIAL-1", _job())
    assert seen == [False]

    job = _job()
    assert job.switch_after_download is False
    provider = _provider(
        AndroidAuthorizationSnapshot(euicc_enabled=True, has_carrier_privileges=True)
    )
    result = provider.activate_profile("SERIAL-1", job, 12)
    assert result.accepted is False
    assert result.activation_code_sent is False
    assert "switch_after_download" in (result.error or "")


def test_partial_never_becomes_success():
    result = _provider(AndroidAuthorizationSnapshot()).verify_profile(
        "SERIAL-1", _job(), _layers()
    )
    assert result.verdict is ActivationVerdict.ACTIVATION_PARTIAL
    assert result.success is False


def test_unknown_never_becomes_success():
    result = _provider(AndroidAuthorizationSnapshot()).verify_profile(
        "SERIAL-1",
        _job(),
        _layers(settings_visible=None, observation_complete=False),
    )
    assert result.verdict is ActivationVerdict.VERIFICATION_UNKNOWN
    assert result.success is False


def test_failed_never_becomes_success():
    result = _provider(AndroidAuthorizationSnapshot()).verify_profile(
        "SERIAL-1",
        _job(),
        _layers(explicit_failure=True, failure_reason="download rejected"),
    )
    assert result.verdict is ActivationVerdict.ACTIVATION_FAILED
    assert result.success is False


def test_confirmed_only_is_success():
    result = _provider(AndroidAuthorizationSnapshot()).verify_profile(
        "SERIAL-1",
        _job(),
        _layers(subscription_embedded=True, data_registered=True, cellular=True),
    )
    assert result.verdict is ActivationVerdict.ACTIVATION_CONFIRMED
    assert result.success is True


def test_armed_without_transport_does_not_send_code():
    provider = _provider(
        AndroidAuthorizationSnapshot(euicc_enabled=True, has_carrier_privileges=True),
        live_download_armed=True,
    )
    result = provider.submit_activation("SERIAL-1", _job())
    assert result.accepted is False
    assert result.activation_code_sent is False
    assert "transport" in (result.error or "")


def test_live_download_may_arm_requires_allowlist_one_and_both_flags():
    from infrastructure.authorized_esim_provider import live_download_may_arm

    assert live_download_may_arm(
        real_esim_enabled=True,
        esim_live_download_armed=True,
        allowed_slot_ids=frozenset({1}),
    ) is True
    assert live_download_may_arm(
        real_esim_enabled=True,
        esim_live_download_armed=False,
        allowed_slot_ids=frozenset({1}),
    ) is False
    assert live_download_may_arm(
        real_esim_enabled=False,
        esim_live_download_armed=True,
        allowed_slot_ids=frozenset({1}),
    ) is False
    assert live_download_may_arm(
        real_esim_enabled=True,
        esim_live_download_armed=True,
        allowed_slot_ids=frozenset({1, 2}),
    ) is False


def test_armed_transport_sends_without_logging_or_persisting(caplog, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sent: list[tuple[str, object]] = []

    def transport(serial, job):
        sent.append((serial, job.slot_id))
        _ = job.activation_code
        return {"success": True, "device_code": 0, "esim_state": "installed"}

    provider = _provider(
        AndroidAuthorizationSnapshot(euicc_enabled=True, device_owner=True),
        live_download_armed=True,
        download_transport=transport,
        verification_source=lambda serial, job: _layers(
            subscription_embedded=True,
            data_registered=True,
            cellular=True,
            settings_visible=True,
        ),
    )
    with caplog.at_level(logging.DEBUG):
        submitted = provider.submit_activation("SERIAL-1", _job())
        result = provider.provision("SERIAL-1", _job())
    assert submitted.accepted is True
    assert submitted.activation_code_sent is True
    assert sent == [("SERIAL-1", 1), ("SERIAL-1", 1)]
    assert SECRET not in caplog.text
    assert result.success is True
    assert result.verdict is ActivationVerdict.ACTIVATION_CONFIRMED
    assert SECRET not in str(result.to_dict())


def test_armed_transport_partial_is_not_success():
    provider = _provider(
        AndroidAuthorizationSnapshot(euicc_enabled=True, device_owner=True),
        live_download_armed=True,
        download_transport=lambda serial, job: {"success": True, "device_code": 0},
        verification_source=lambda serial, job: _layers(),
    )
    result = provider.provision("SERIAL-1", _job())
    assert result.verdict is ActivationVerdict.ACTIVATION_PARTIAL
    assert result.success is False


def test_select_provider_uses_human_without_authority_even_if_flag_true():
    provider = select_esim_provider(
        AndroidAuthorizationSnapshot(euicc_enabled=True, real_esim_flag=True),
        SlotIsolationPolicy({1}),
        live_download_armed=True,
    )
    assert isinstance(provider, HumanActivationProvider)
    assert provider.capabilities().unattended is False


def test_select_provider_returns_authorized_only_after_capability_gate():
    provider = select_esim_provider(
        AndroidAuthorizationSnapshot(euicc_enabled=True, has_carrier_privileges=True),
        SlotIsolationPolicy({1}),
        live_download_armed=False,
    )
    assert isinstance(provider, AuthorizedEsimProvider)
    submitted = provider.submit_activation("SERIAL-1", _job())
    assert submitted.accepted is False
    assert submitted.activation_code_sent is False
