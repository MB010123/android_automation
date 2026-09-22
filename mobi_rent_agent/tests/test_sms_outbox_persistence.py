from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from application.sms_outbox import DuplicateIdempotencyError, SmsOutbox
from application.sms_service import SmsDispatchService
from domain.farm import OutboundMessageStatus, SmsFinalStatus
from domain.models import InboundSms, SmsSendOutcome, SmsSendResult
from domain.slot_isolation import SlotIsolationPolicy
from infrastructure.sms_outbox_store import SqliteSmsOutboxStore, SCHEMA_VERSION
from infrastructure.voidfix_delivery import DeliveryPollResult, VoidFixMessageSnapshot


class FakeGateway:
    def __init__(self) -> None:
        self.send_calls = 0

    def send(self, to_number, message, device_ids, *, sim_slot=None):
        self.send_calls += 1
        raise AssertionError("send must not be called during restart recovery tests")

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


def _store(tmp_path: Path) -> SqliteSmsOutboxStore:
    return SqliteSmsOutboxStore(tmp_path / "sms_outbox.sqlite")


def _service(outbox: SmsOutbox, gateway: FakeGateway, poller: FakePoller | None = None):
    return SmsDispatchService(
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


def test_schema_migration_on_empty_db(tmp_path: Path):
    store = _store(tmp_path)
    row = store._conn.execute("SELECT version FROM schema_version").fetchone()
    assert int(row["version"]) == SCHEMA_VERSION


def test_queued_record_survives_restart(tmp_path: Path):
    store = _store(tmp_path)
    outbox1 = SmsOutbox(store=store)
    outbox1.reserve(slot_id=1, to_number="+15551234567", idempotency_key="q1")
    outbox2 = SmsOutbox(store=store)
    record = outbox2.get("q1")
    assert record is not None
    assert record.final_status is SmsFinalStatus.QUEUED


def test_accepted_record_restored_not_resent(tmp_path: Path):
    store = _store(tmp_path)
    outbox1 = SmsOutbox(store=store)
    outbox1.reserve(slot_id=1, to_number="+15551234567", idempotency_key="a1")
    outbox1.update(
        "a1",
        status=OutboundMessageStatus.CONFIRMED,
        provider_message_id="9001",
        voidfix_device_id="1386",
        voidfix_sim_slot=1,
        final_status=SmsFinalStatus.ACCEPTED,
        accepted_at=1.0,
    )
    gateway = FakeGateway()
    outbox2 = SmsOutbox(store=store)
    service = _service(outbox2, gateway)
    result = service.send_for_slot(1, "+15551234567", "hello", idempotency_key="a1")
    assert result.success is False
    assert gateway.send_calls == 0
    assert outbox2.get("a1").provider_message_id == "9001"


def test_sent_record_resumes_delivery_poll(tmp_path: Path):
    store = _store(tmp_path)
    outbox1 = SmsOutbox(store=store)
    outbox1.reserve(slot_id=1, to_number="+15551234567", idempotency_key="s1")
    outbox1.update(
        "s1",
        status=OutboundMessageStatus.SENT,
        provider_message_id="9002",
        voidfix_device_id="1386",
        voidfix_sim_slot=1,
        final_status=SmsFinalStatus.SENT,
        provider_sent_date="2026-01-01T00:00:01+0000",
    )
    snap = VoidFixMessageSnapshot(
        message_id="9002",
        status="Sent",
        device_id="1386",
        sim_slot=1,
        destination="+15551234567",
        sent_date="2026-01-01T00:00:01+0000",
        delivered_date="2026-01-01T00:00:05+0000",
        error_code="-1",
        raw={"ID": 9002, "deliveredDate": "2026-01-01T00:00:05+0000"},
    )
    poller = FakePoller(DeliveryPollResult(final_status=SmsFinalStatus.DELIVERED, snapshot=snap))
    gateway = FakeGateway()
    outbox2 = SmsOutbox(store=store)
    service = _service(outbox2, gateway, poller=poller)
    resumed = service.resume_inflight_delivery_tracking()
    assert resumed == ["s1"]
    assert poller.calls == 1
    assert gateway.send_calls == 0
    record = outbox2.get("s1")
    assert record.final_status is SmsFinalStatus.DELIVERED
    assert record.provider_delivered_date is not None


def test_delivered_record_stays_terminal(tmp_path: Path):
    store = _store(tmp_path)
    outbox1 = SmsOutbox(store=store)
    outbox1.reserve(slot_id=1, to_number="+15551234567", idempotency_key="d1")
    outbox1.update(
        "d1",
        status=OutboundMessageStatus.DELIVERED,
        provider_message_id="9003",
        voidfix_device_id="1386",
        voidfix_sim_slot=1,
        final_status=SmsFinalStatus.DELIVERED,
        provider_delivered_date="2026-01-01T00:00:05+0000",
    )
    outbox2 = SmsOutbox(store=store)
    poller = FakePoller(
        DeliveryPollResult(final_status=SmsFinalStatus.DELIVERED, snapshot=None)
    )
    gateway = FakeGateway()
    service = _service(outbox2, gateway, poller=poller)
    assert service.resume_inflight_delivery_tracking() == []
    assert poller.calls == 0
    with pytest.raises(DuplicateIdempotencyError):
        outbox2.reserve(slot_id=1, to_number="+15551234567", idempotency_key="d1")


def test_idempotency_key_rejected_after_restart(tmp_path: Path):
    store = _store(tmp_path)
    outbox1 = SmsOutbox(store=store)
    outbox1.reserve(slot_id=1, to_number="+15551234567", idempotency_key="idem-1")
    outbox2 = SmsOutbox(store=store)
    with pytest.raises(DuplicateIdempotencyError):
        outbox2.reserve(slot_id=1, to_number="+15551234567", idempotency_key="idem-1")


def test_failed_and_timeout_persist(tmp_path: Path):
    store = _store(tmp_path)
    outbox = SmsOutbox(store=store)
    outbox.reserve(slot_id=1, to_number="+15551234567", idempotency_key="f1")
    outbox.update(
        "f1",
        status=OutboundMessageStatus.FAILED,
        final_status=SmsFinalStatus.FAILED,
        provider_message_id="9004",
        error="failed",
    )
    outbox.reserve(slot_id=1, to_number="+15551234567", idempotency_key="t1")
    outbox.update(
        "t1",
        status=OutboundMessageStatus.TIMEOUT,
        final_status=SmsFinalStatus.TIMEOUT,
        provider_message_id="9005",
        error="timeout",
    )
    reloaded = SmsOutbox(store=store)
    assert reloaded.get("f1").final_status is SmsFinalStatus.FAILED
    assert reloaded.get("t1").final_status is SmsFinalStatus.TIMEOUT
