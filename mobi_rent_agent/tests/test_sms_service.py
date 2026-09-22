from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from application.slot_coordinator import SlotOperationCoordinator
from application.sms_service import SmsDispatchService
from domain.models import InboundSms, SmsSendResult
from domain.slot_isolation import SlotIsolationPolicy
from infrastructure.voidfix_devices import (
    VoidFixDeviceMapError,
    load_voidfix_device_map,
    load_voidfix_sim_slots,
)


class FakeGateway:
    def __init__(self, result: SmsSendResult | None = None, inbound=None) -> None:
        self.result = result or SmsSendResult(success=True, to_number="+1", provider_message_id="x")
        self.inbound = inbound or []
        self.send_calls: list[tuple] = []
        self.fetch_calls = 0
        self.ingest_payloads: list[object] = []

    def send(self, to_number, message, device_ids, *, sim_slot=None):
        self.send_calls.append((to_number, message, list(device_ids), sim_slot))
        return self.result

    def fetch_inbound(self):
        self.fetch_calls += 1
        return list(self.inbound)

    def ingest_inbound(self, payload):
        self.ingest_payloads.append(payload)
        return [InboundSms(from_number="+1", message="ok")]


def make_service(**kwargs) -> tuple[SmsDispatchService, FakeGateway]:
    gateway = kwargs.pop("gateway", None) or FakeGateway()
    service = SmsDispatchService(
        gateway=gateway,
        isolation=kwargs.get("isolation", SlotIsolationPolicy({1})),
        device_map=kwargs.get("device_map", {1: "215"}),
        coordinator=kwargs.get("coordinator"),
        webhook_secret=kwargs.get("webhook_secret"),
        recipient_allowlist=kwargs.get("recipient_allowlist", ("+15551234567", "+1")),
        live_send_authorized=kwargs.get("live_send_authorized", True),
        dry_run=kwargs.get("dry_run", False),
        outbox=kwargs.get("outbox"),
        identity_guard=kwargs.get("identity_guard"),
        retry_policy=kwargs.get("retry_policy"),
        sleeper=kwargs.get("sleeper"),
    )
    return service, gateway


def test_send_for_slot_uses_allowlisted_device_id():
    service, gateway = make_service()

    result = service.send_for_slot(1, "+15551234567", "hello")

    assert result.success is True
    assert gateway.send_calls == [("+15551234567", "hello", ["215"], None)]


def test_send_for_slot_refuses_outside_allowlist_without_calling_gateway():
    service, gateway = make_service()

    result = service.send_for_slot(2, "+15551234567", "hello")

    assert result.success is False
    assert "allowlist" in (result.error or "")
    assert gateway.send_calls == []


def test_empty_allowlist_refuses_slot_1():
    service, gateway = make_service(isolation=SlotIsolationPolicy(()), device_map={1: "215"})

    result = service.send_for_slot(1, "+15551234567", "hello")

    assert result.success is False
    assert gateway.send_calls == []


def test_missing_device_mapping_does_not_fall_back_to_another_slot():
    service, gateway = make_service(
        isolation=SlotIsolationPolicy({1, 2}),
        device_map={2: "216"},
    )

    result = service.send_for_slot(1, "+15551234567", "hello")

    assert result.success is False
    assert "device_id" in (result.error or "")
    assert gateway.send_calls == []


def test_allowed_slot_uses_only_its_mapped_device():
    service, gateway = make_service(
        isolation=SlotIsolationPolicy({1, 2}),
        device_map={1: "215", 2: "216"},
    )

    result = service.send_for_slot(1, "+15551234567", "hello")

    assert result.success is True
    assert gateway.send_calls == [("+15551234567", "hello", ["215"], None)]


def test_missing_device_id_refuses_send():
    service, gateway = make_service(device_map={})

    result = service.send_for_slot(1, "+15551234567", "hello")

    assert result.success is False
    assert "device_id" in (result.error or "")
    assert gateway.send_calls == []


def test_coordinator_busy_slot_does_not_send():
    coordinator = SlotOperationCoordinator([1])
    service, gateway = make_service(coordinator=coordinator)
    with coordinator.acquire(1):
        result = service.send_for_slot(1, "+15551234567", "hello")

    assert result.success is False
    assert "busy" in (result.error or "")
    assert gateway.send_calls == []


def test_webhook_secret_mismatch_rejects_payload():
    service, gateway = make_service(webhook_secret="expected")

    with pytest.raises(ValueError, match="secret mismatch"):
        service.ingest_inbound({"number": "+1", "message": "x"}, provided_secret="wrong")
    assert gateway.ingest_payloads == []


def test_webhook_rejects_missing_secret_when_required():
    service, gateway = make_service(webhook_secret="expected")

    with pytest.raises(ValueError, match="secret mismatch"):
        service.ingest_inbound({"number": "+1", "message": "x"})
    assert gateway.ingest_payloads == []


def test_webhook_secret_match_parses_payload():
    service, gateway = make_service(webhook_secret="expected")

    messages = service.ingest_inbound({"number": "+1", "message": "x"}, provided_secret="expected")

    assert len(messages) == 1
    assert gateway.ingest_payloads == [{"number": "+1", "message": "x"}]


def test_load_voidfix_device_map_parses_string_and_object(tmp_path: Path):
    path = tmp_path / "voidfix_devices.json"
    path.write_text(json.dumps({"1": "215", "2": {"device_id": "216"}}), encoding="utf-8")

    mapping = load_voidfix_device_map(path)

    assert mapping == {1: "215", 2: "216"}


def test_load_voidfix_device_map_rejects_duplicate_device_ids(tmp_path: Path):
    path = tmp_path / "voidfix_devices.json"
    path.write_text(json.dumps({"1": "215", "2": "215"}), encoding="utf-8")

    with pytest.raises(VoidFixDeviceMapError, match="duplicate VoidFix device_id"):
        load_voidfix_device_map(path)


def test_load_voidfix_device_map_rejects_invalid_slot_ids(tmp_path: Path):
    path = tmp_path / "voidfix_devices.json"
    path.write_text(json.dumps({"0": "215"}), encoding="utf-8")
    with pytest.raises(VoidFixDeviceMapError, match="1-20"):
        load_voidfix_device_map(path)

    path.write_text(json.dumps({"abc": "215"}), encoding="utf-8")
    with pytest.raises(VoidFixDeviceMapError, match="slot ids"):
        load_voidfix_device_map(path)


def test_load_voidfix_device_map_rejects_adb_serial_lookalike(tmp_path: Path):
    path = tmp_path / "voidfix_devices.json"
    path.write_text(json.dumps({"1": "1C101FDF6009EZ"}), encoding="utf-8")

    with pytest.raises(VoidFixDeviceMapError, match="ADB serial"):
        load_voidfix_device_map(path)


def test_missing_device_map_path_is_empty():
    assert load_voidfix_device_map(None) == {}


def test_missing_device_map_file_raises(tmp_path: Path):
    with pytest.raises(VoidFixDeviceMapError, match="not found"):
        load_voidfix_device_map(tmp_path / "missing.json")


def test_load_voidfix_sim_slots_reads_verified_selector_only(tmp_path: Path):
    path = tmp_path / "voidfix_devices.json"
    path.write_text(
        json.dumps({"1": {"device_id": "215", "sim_slot": 1}, "2": "216"}),
        encoding="utf-8",
    )
    assert load_voidfix_sim_slots(path) == {1: 1}


def test_load_voidfix_sim_slots_rejects_invalid(tmp_path: Path):
    path = tmp_path / "voidfix_devices.json"
    path.write_text(json.dumps({"1": {"device_id": "215", "sim_slot": 9}}), encoding="utf-8")
    with pytest.raises(VoidFixDeviceMapError, match="sim_slot"):
        load_voidfix_sim_slots(path)
