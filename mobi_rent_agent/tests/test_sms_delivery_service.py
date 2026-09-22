from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from application.sms_outbox import SmsOutbox
from application.sms_service import SmsDispatchService
from domain.farm import OutboundMessageStatus, SmsFinalStatus, VoidFixDeliveryPollPolicy
from domain.models import InboundSms, SmsSendOutcome, SmsSendResult
from domain.slot_isolation import SlotIsolationPolicy
from infrastructure.voidfix_delivery import DeliveryPollResult, VoidFixMessageSnapshot


class FakeGateway:
    def __init__(self, result: SmsSendResult) -> None:
        self.result = result
        self.send_calls: list[dict] = []

    def send(self, to_number, message, device_ids, *, sim_slot=None):
        self.send_calls.append(
            {"to": to_number, "message": message, "devices": list(device_ids), "sim_slot": sim_slot}
        )
        return self.result

    def fetch_inbound(self):
        return []

    def ingest_inbound(self, payload):
        return [InboundSms(from_number="+1", message="x")]


class FakePoller:
    def __init__(self, result: DeliveryPollResult) -> None:
        self.result = result
        self.calls = 0

    def poll_until_terminal(self, provider_message_id, **kwargs):
        self.calls += 1
        on_poll = kwargs.get("on_poll")
        if on_poll is not None and self.result.snapshot is not None:
            on_poll(self.result.snapshot)
        return self.result


def test_send_with_delivery_poll_updates_outbox_to_delivered():
    accept_row = {
        "ID": 99,
        "deviceID": 1386,
        "simSlot": 1,
        "sentDate": "2026-01-01T00:00:00+0000",
        "status": "Pending",
    }
    gateway = FakeGateway(
        SmsSendResult(
            success=True,
            to_number="+15551234567",
            provider_message_id="99",
            provider_accept_row=accept_row,
            voidfix_sim_slot=1,
        )
    )
    snap = VoidFixMessageSnapshot(
        message_id="99",
        status="Sent",
        device_id="1386",
        sim_slot=1,
        destination="+15551234567",
        sent_date="2026-01-01T00:00:01+0000",
        delivered_date="2026-01-01T00:00:02+0000",
        error_code="-1",
        raw={"ID": 99, "status": "Sent", "deliveredDate": "2026-01-01T00:00:02+0000"},
    )
    poller = FakePoller(DeliveryPollResult(final_status=SmsFinalStatus.DELIVERED, snapshot=snap))
    outbox = SmsOutbox()
    service = SmsDispatchService(
        gateway=gateway,
        isolation=SlotIsolationPolicy({1}),
        device_map={1: "1386"},
        recipient_allowlist=("+15551234567",),
        live_send_authorized=True,
        dry_run=False,
        outbox=outbox,
        delivery_poller=poller,
        voidfix_sim_slots={1: 1},
    )

    result = service.send_for_slot(1, "+15551234567", "hello", idempotency_key="k-deliver")
    record = outbox.get("k-deliver")

    assert result.success is True
    assert gateway.send_calls[0]["sim_slot"] == 1
    assert record.final_status is SmsFinalStatus.DELIVERED
    assert record.status is OutboundMessageStatus.DELIVERED
    assert record.provider_delivered_date is not None
    assert record.voidfix_device_id == "1386"


def test_delivery_poll_failed_marks_outbox_failed():
    gateway = FakeGateway(
        SmsSendResult(
            success=True,
            to_number="+15551234567",
            provider_message_id="100",
            provider_accept_row={"ID": 100, "status": "Pending"},
        )
    )
    snap = VoidFixMessageSnapshot(
        message_id="100",
        status="Failed",
        device_id="1386",
        sim_slot=1,
        destination="+15551234567",
        sent_date=None,
        delivered_date=None,
        error_code="7",
        raw={"ID": 100, "status": "Failed", "errorCode": 7},
    )
    poller = FakePoller(DeliveryPollResult(final_status=SmsFinalStatus.FAILED, snapshot=snap))
    outbox = SmsOutbox()
    service = SmsDispatchService(
        gateway=gateway,
        isolation=SlotIsolationPolicy({1}),
        device_map={1: "1386"},
        recipient_allowlist=("+15551234567",),
        live_send_authorized=True,
        dry_run=False,
        outbox=outbox,
        delivery_poller=poller,
    )

    result = service.send_for_slot(1, "+15551234567", "hello", idempotency_key="k-fail")
    record = outbox.get("k-fail")
    assert result.success is False
    assert record.final_status is SmsFinalStatus.FAILED
    assert record.status is OutboundMessageStatus.FAILED


def test_delivery_poll_timeout_marks_timeout():
    gateway = FakeGateway(
        SmsSendResult(
            success=True,
            to_number="+15551234567",
            provider_message_id="101",
            provider_accept_row={"ID": 101, "status": "Pending"},
        )
    )
    poller = FakePoller(DeliveryPollResult(final_status=SmsFinalStatus.TIMEOUT, snapshot=None, timed_out=True))
    outbox = SmsOutbox()
    service = SmsDispatchService(
        gateway=gateway,
        isolation=SlotIsolationPolicy({1}),
        device_map={1: "1386"},
        recipient_allowlist=("+15551234567",),
        live_send_authorized=True,
        dry_run=False,
        outbox=outbox,
        delivery_poller=poller,
    )

    result = service.send_for_slot(1, "+15551234567", "hello", idempotency_key="k-timeout")
    record = outbox.get("k-timeout")
    assert result.outcome is SmsSendOutcome.UNKNOWN
    assert record.final_status is SmsFinalStatus.TIMEOUT
    assert record.status is OutboundMessageStatus.TIMEOUT
