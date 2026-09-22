from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from application.prototype_service import PrototypeRefuse, PrototypeService
from infrastructure.prototype_voidfix_api import (
    HARMLESS_TEST_SMS,
    MISSING_LIVE_SLOT_REASON,
    PrototypeVoidFixRestClient,
)
from domain.esim_capabilities import AndroidAuthorizationSnapshot
from domain.models import SmsSendOutcome, SmsSendResult
from domain.prototype import PrototypeMode
from domain.provisioning_state import ActivationVerdict
from domain.verification import (
    ConnectivityLayer,
    FourLayerVerification,
    SettingsLpaLayer,
    SubscriptionLayer,
    TelephonyLayer,
)
from infrastructure.prototype_audit import PrototypeAuditStore
from infrastructure.prototype_config import PrototypeConfig, PrototypeDevice

FARM_SERIAL = "1C101FDF6009EZ"
RECIPIENT = "+15555550100"


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def send(self, to_number, message, device_ids):
        self.calls.append((to_number, message, list(device_ids)))
        return SmsSendResult(
            success=True,
            to_number=to_number,
            provider_message_id="msg-proto-1",
            status_code=200,
            outcome=SmsSendOutcome.SUCCESS,
        )

    def fetch_inbound(self):
        raise AssertionError("inbound poll must stay disabled")

    def ingest_inbound(self, payload):
        _ = payload
        return []


def _device(**overrides) -> PrototypeDevice:
    values = dict(
        device_id="prototype-device-1",
        device_type="pixel_7a",
        adb_serial="TESTSERIAL7A",
        voidfix_device_id="1385",
        enabled=True,
        role="test_pixel_7a",
    )
    values.update(overrides)
    return PrototypeDevice(**values)


def _config(**overrides) -> PrototypeConfig:
    device = overrides.pop("device", _device())
    values = dict(
        environment="prototype",
        mode=PrototypeMode.PRODUCTION_DISABLED,
        devices={device.device_id: device},
        allowlist=(device.device_id,),
        recipient_allowlist=(RECIPIENT,),
        voidfix_enabled=False,
        voidfix_api_key=None,
        voidfix_send_endpoint="https://sms.voidfix.com/services/send.php",
        voidfix_inbound_endpoint=None,
        voidfix_webhook_secret=None,
        real_send_confirmation=False,
        log_dir=Path("logs/prototype"),
        production_serials=frozenset({FARM_SERIAL}),
        inbound_documented=False,
    )
    values.update(overrides)
    if "devices" not in overrides:
        values["devices"] = {device.device_id: device}
    return PrototypeConfig(**values)


def _confirmed() -> FourLayerVerification:
    return FourLayerVerification(
        settings_lpa=SettingsLpaLayer(euicc_enabled=True, settings_profile_visible=True),
        subscription=SubscriptionLayer(
            subscription_present=True,
            subscription_embedded=True,
            default_data_sub_id=2,
        ),
        telephony=TelephonyLayer(data_registered=True, emergency_only=False),
        connectivity=ConnectivityLayer(
            wifi_enabled=False,
            cellular_transport_available=True,
            cellular_ip_present=True,
            default_route_cellular=True,
            internet_proves_cellular=True,
        ),
    )


def _partial() -> FourLayerVerification:
    return FourLayerVerification(
        settings_lpa=SettingsLpaLayer(euicc_enabled=True, settings_profile_visible=True),
        subscription=SubscriptionLayer(subscription_present=False, subscription_embedded=False),
        telephony=TelephonyLayer(data_registered=False, emergency_only=True),
        connectivity=ConnectivityLayer(wifi_enabled=True, cellular_transport_available=False),
    )


def test_unknown_device_rejected():
    service = PrototypeService(_config())
    with pytest.raises(PrototypeRefuse, match="unknown prototype device"):
        service.device_status("no-such-device")


def test_production_serial_rejected_at_runtime():
    device = _device(adb_serial=FARM_SERIAL)
    service = PrototypeService(_config(device=device, allowlist=(device.device_id,)))
    with pytest.raises(PrototypeRefuse, match="production farm serial"):
        service.device_status(device.device_id)


def test_missing_mapping_and_no_fallback():
    mapped = _device(device_id="prototype-device-2", adb_serial="OTHER", voidfix_device_id="216")
    unmapped = _device(voidfix_device_id=None)
    config = _config(
        mode=PrototypeMode.REAL_TEST,
        voidfix_enabled=True,
        voidfix_api_key="test-key",
        real_send_confirmation=True,
        devices={unmapped.device_id: unmapped, mapped.device_id: mapped},
        allowlist=(unmapped.device_id, mapped.device_id),
    )
    gateway = FakeGateway()
    service = PrototypeService(config, gateway=gateway)
    with pytest.raises(PrototypeRefuse, match="no VoidFix device mapping"):
        service.send_test_sms(unmapped.device_id, RECIPIENT, confirm=True)
    assert gateway.calls == []


def test_real_send_gate_requires_every_flag():
    gateway = FakeGateway()
    service = PrototypeService(_config(mode=PrototypeMode.REAL_TEST), gateway=gateway)
    with pytest.raises(PrototypeRefuse, match="VOIDFIX_ENABLED"):
        service.send_test_sms("prototype-device-1", RECIPIENT, confirm=True)
    assert gateway.calls == []


def test_authorized_send_requires_live_slot_1_and_calls_gateway_once(tmp_path: Path):
    audit = PrototypeAuditStore(tmp_path / "audit.jsonl")
    gateway = FakeGateway()
    service = PrototypeService(
        _config(
            mode=PrototypeMode.REAL_TEST,
            voidfix_enabled=True,
            voidfix_api_key="test-key",
            real_send_confirmation=True,
        ),
        gateway=gateway,
        audit=audit,
    )
    first = service.send_test_sms(
        "prototype-device-1",
        RECIPIENT,
        confirm=True,
        operation_id="op-1",
        android_sim_slot_index=1,
    )
    second = service.send_test_sms(
        "prototype-device-1",
        RECIPIENT,
        confirm=True,
        operation_id="op-1",
        android_sim_slot_index=1,
    )
    assert first["sms_sent"] is True
    assert first["provider_message_id"] == "msg-proto-1"
    assert gateway.calls == [(RECIPIENT, HARMLESS_TEST_SMS, ["1385"])]
    assert second["duplicate_prevented"] is True
    assert second["sms_sent"] is False
    assert len(gateway.calls) == 1


def test_missing_live_slot_refuses_without_hardcoding():
    gateway = FakeGateway()
    service = PrototypeService(
        _config(
            mode=PrototypeMode.REAL_TEST,
            voidfix_enabled=True,
            voidfix_api_key="test-key",
            real_send_confirmation=True,
        ),
        gateway=gateway,
    )
    with pytest.raises(PrototypeRefuse, match="live Android simSlotIndex is required"):
        service.send_test_sms("prototype-device-1", RECIPIENT, confirm=True)
    assert gateway.calls == []
    assert MISSING_LIVE_SLOT_REASON


def test_service_posts_confirmed_json_slot_through_prototype_client():
    class RecordingSession:
        def __init__(self) -> None:
            self.post_calls: list[dict] = []

        def post(self, url, data=None, json=None, headers=None, timeout=None):
            self.post_calls.append({"url": url, "data": data, "json": json, "headers": headers})

            class _Response:
                status_code = 200
                text = ""

                def json(self):
                    return {"success": True, "id": "vf-slot-1"}

            return _Response()

    session = RecordingSession()
    client = PrototypeVoidFixRestClient(api_key="test-key", session=session)
    service = PrototypeService(
        _config(
            mode=PrototypeMode.REAL_TEST,
            voidfix_enabled=True,
            voidfix_api_key="test-key",
            real_send_confirmation=True,
        ),
        gateway=client,
    )
    result = service.send_test_sms(
        "prototype-device-1",
        RECIPIENT,
        confirm=True,
        android_sim_slot_index=1,
    )
    assert result["sms_sent"] is True
    assert result["provider_message_id"] == "vf-slot-1"
    sent = session.post_calls[0]
    assert sent["data"] is None
    assert sent["headers"]["Content-Type"] == "application/json"
    assert sent["json"]["devices"] == [1385]
    assert sent["json"]["slot"] == 1
    assert "sim" not in sent["json"]
    assert sent["json"]["key"] == "test-key"


def test_preview_does_not_print_full_number():
    preview = PrototypeService(_config()).preview_send("prototype-device-1", RECIPIENT)
    assert preview["Recipient"] == "+XXX****0100"
    assert RECIPIENT not in json.dumps(preview)


def test_dry_run_human_activation_does_not_send_or_retry():
    service = PrototypeService(_config(mode=PrototypeMode.DRY_RUN))
    result = service.dry_run("prototype-device-1", verification=_partial())
    assert result["sms_sent"] is False
    assert result["mutations"] is False
    assert result["activation_code_sent"] is False
    assert result["automatic_retries"] is False
    assert "retryable" not in result["job_states"]
    assert result["verification_verdict"] == ActivationVerdict.ACTIVATION_PARTIAL.value
    assert result["success"] is False


def test_activation_confirmed_is_only_success():
    service = PrototypeService(_config(mode=PrototypeMode.DRY_RUN))
    result = service.dry_run("prototype-device-1", verification=_confirmed())
    assert result["verification_verdict"] == ActivationVerdict.ACTIVATION_CONFIRMED.value
    assert result["success"] is True
    assert result["sms_sent"] is False


def test_device_owner_missing_and_mismatch():
    service = PrototypeService(_config())
    missing = service.device_status(
        "prototype-device-1",
        snapshot=AndroidAuthorizationSnapshot(euicc_feature=True, euicc_enabled=True),
        device_policy_dump="Device Owner:\n    none\n  mDeviceOwner=null\n",
    )
    assert missing["device_owner"] is False
    assert missing["can_silent_install"] is False
    assert missing["status"] == "requires_authorization"

    mismatch = service.device_status(
        "prototype-device-1",
        snapshot=AndroidAuthorizationSnapshot(device_owner=True, euicc_enabled=True),
        device_policy_dump=(
            "Device Owner:\n    admin=ComponentInfo{com.other.mdm/.Admin}\n"
            "    package=com.other.mdm\n"
        ),
    )
    assert mismatch["status"] == "dpc_package_mismatch"
    assert mismatch["dpc_is_device_owner"] is False


def test_device_owner_detected_for_companion_package():
    service = PrototypeService(_config())
    report = service.device_status(
        "prototype-device-1",
        snapshot=AndroidAuthorizationSnapshot(
            device_owner=True,
            euicc_feature=True,
            euicc_enabled=True,
            organization_owned=True,
        ),
        device_policy_dump=(
            "Device Owner:\n    admin=ComponentInfo{com.mobirent.companion/.DeviceAdminReceiver}\n"
            "    package=com.mobirent.companion\n"
        ),
    )
    assert report["dpc_is_device_owner"] is True
    assert report["can_silent_install"] is True
    assert report["status"] in {"authorized", "device_owner_present"}


def test_cellular_unavailable_is_not_confirmed():
    service = PrototypeService(_config())
    status = service.esim_status(
        "prototype-device-1",
        snapshot=AndroidAuthorizationSnapshot(euicc_enabled=True),
        verification=_partial(),
    )
    assert status["verification_verdict"] == ActivationVerdict.ACTIVATION_PARTIAL.value
    assert status["success"] is False
    assert status["automatic_retries"] is False


def test_audit_and_disabled_voidfix_send_nothing():
    gateway = FakeGateway()
    service = PrototypeService(_config(mode=PrototypeMode.AUDIT), gateway=gateway)
    report = service.audit()
    assert report["sms_sent"] is False
    assert report["mutations"] is False
    with pytest.raises(PrototypeRefuse):
        service.send_test_sms("prototype-device-1", RECIPIENT, confirm=True)
    assert gateway.calls == []


def test_production_slot_map_bytes_unchanged(tmp_path: Path):
    slot_map = Path(__file__).resolve().parents[1] / "slot_map.json"
    before = slot_map.read_bytes() if slot_map.exists() else None
    PrototypeService(_config(mode=PrototypeMode.DRY_RUN)).dry_run("prototype-device-1")
    after = slot_map.read_bytes() if slot_map.exists() else None
    assert before == after
    _ = tmp_path


def test_non_1385_voidfix_id_is_rejected():
    gateway = FakeGateway()
    service = PrototypeService(
        _config(
            device=_device(voidfix_device_id="215"),
            mode=PrototypeMode.REAL_TEST,
            voidfix_enabled=True,
            voidfix_api_key="test-key",
            real_send_confirmation=True,
        ),
        gateway=gateway,
    )
    with pytest.raises(PrototypeRefuse, match="VoidFix device 1385"):
        service.send_test_sms(
            "prototype-device-1",
            RECIPIENT,
            confirm=True,
            android_sim_slot_index=1,
        )
    assert gateway.calls == []


def test_farm_slot_ids_are_rejected():
    service = PrototypeService(_config())
    with pytest.raises(PrototypeRefuse, match="production farm slot ID"):
        service.send_test_sms("1", RECIPIENT, confirm=True)


def test_non_prototype_device_is_rejected_even_when_mapped():
    other = _device(device_id="prototype-device-2", adb_serial="OTHER", voidfix_device_id="1385")
    gateway = FakeGateway()
    service = PrototypeService(
        _config(
            device=other,
            mode=PrototypeMode.REAL_TEST,
            voidfix_enabled=True,
            voidfix_api_key="test-key",
            real_send_confirmation=True,
            allowlist=(other.device_id,),
        ),
        gateway=gateway,
    )
    with pytest.raises(PrototypeRefuse, match="isolated prototype device"):
        service.send_test_sms(other.device_id, RECIPIENT, confirm=True)
    assert gateway.calls == []


def test_wrong_android_slot_is_refused_and_never_falls_back_to_zero():
    gateway = FakeGateway()
    service = PrototypeService(
        _config(
            mode=PrototypeMode.REAL_TEST,
            voidfix_enabled=True,
            voidfix_api_key="test-key",
            real_send_confirmation=True,
        ),
        gateway=gateway,
    )
    with pytest.raises(PrototypeRefuse, match="slot 0"):
        service.send_test_sms(
            "prototype-device-1",
            RECIPIENT,
            confirm=True,
            android_sim_slot_index=0,
        )
    with pytest.raises(PrototypeRefuse, match="simSlotIndex is 2"):
        service.send_test_sms(
            "prototype-device-1",
            RECIPIENT,
            confirm=True,
            android_sim_slot_index=2,
        )
    assert gateway.calls == []


def test_voidfix_status_reports_confirmed_slot_contract():
    status = PrototypeService(_config()).voidfix_status()
    assert status["content_type"] == "application/json"
    assert status["confirmed_slot_parameter"] is True
    assert status["required_api_slot"] == 1
    assert status["required_android_sim_slot_index"] == 1
    assert status["slot_requires_live_android_index"] is True
    assert status["sms_sent"] is False
    assert status["automatic_retries"] is False
