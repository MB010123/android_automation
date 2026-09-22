from __future__ import annotations

import threading
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from application.slot_coordinator import SlotOperationCoordinator
from application.slot_identity import SlotIdentityGuard
from application.sms_outbox import SmsOutbox
from application.sms_service import SmsDispatchService
from domain.farm import OutboundMessageStatus, SmsRetryPolicy
from domain.models import SmsSendOutcome, SmsSendResult
from domain.slot_isolation import SlotIsolationPolicy
from infrastructure.redact import redact_phone, redact_serial


class FakeGateway:
    def __init__(self, results=None) -> None:
        self.results = list(results or [SmsSendResult(success=True, to_number="+1", provider_message_id="m1")])
        self.send_calls: list[tuple] = []

    def send(self, to_number, message, device_ids, *, sim_slot=None):
        self.send_calls.append((to_number, message, list(device_ids), sim_slot))
        if not self.results:
            return SmsSendResult(success=True, to_number=to_number, provider_message_id="m-repeat")
        if len(self.results) == 1:
            return self.results[0]
        return self.results.pop(0)

    def fetch_inbound(self):
        return []

    def ingest_inbound(self, payload):
        return []


def service(**kwargs) -> tuple[SmsDispatchService, FakeGateway]:
    gateway = kwargs.pop("gateway", None) or FakeGateway()
    dispatch = SmsDispatchService(
        gateway=gateway,
        isolation=kwargs.get("isolation", SlotIsolationPolicy({1, 2})),
        device_map=kwargs.get("device_map", {1: "215", 2: "216"}),
        coordinator=kwargs.get("coordinator"),
        recipient_allowlist=kwargs.get("recipient_allowlist", ("+15551234567",)),
        live_send_authorized=kwargs.get("live_send_authorized", True),
        dry_run=kwargs.get("dry_run", False),
        outbox=kwargs.get("outbox"),
        identity_guard=kwargs.get("identity_guard"),
        retry_policy=kwargs.get("retry_policy"),
        sleeper=kwargs.get("sleeper", lambda _s: None),
    )
    return dispatch, gateway


def test_dry_run_does_not_call_provider():
    dispatch, gateway = service(dry_run=True, live_send_authorized=True)
    result = dispatch.send_for_slot(1, "+15551234567", "hello")
    assert result.success is False
    assert "dry-run" in (result.error or "")
    assert gateway.send_calls == []


def test_missing_recipient_allowlist_blocks_live_send():
    dispatch, gateway = service(recipient_allowlist=())
    result = dispatch.send_for_slot(1, "+15551234567", "hello")
    assert result.success is False
    assert "allowlist" in (result.error or "")
    assert gateway.send_calls == []


def test_wrong_voidfix_device_id_blocks_send():
    dispatch, gateway = service()
    result = dispatch.send_for_slot(
        1, "+15551234567", "hello", expected_voidfix_device_id="999"
    )
    assert result.success is False
    assert "expected" in (result.error or "")
    assert gateway.send_calls == []


def test_duplicate_idempotency_key_does_not_resend():
    outbox = SmsOutbox()
    dispatch, gateway = service(outbox=outbox)
    first = dispatch.send_for_slot(1, "+15551234567", "hello", idempotency_key="job-1")
    second = dispatch.send_for_slot(1, "+15551234567", "hello", idempotency_key="job-1")
    assert first.success is True
    assert second.success is False
    assert "duplicate" in (second.error or "")
    assert len(gateway.send_calls) == 1


def test_provider_http_error_and_success_false_and_missing_id():
    gateway = FakeGateway(
        [
            SmsSendResult(success=False, to_number="+15551234567", status_code=500, error="http"),
        ]
    )
    dispatch, _ = service(gateway=gateway)
    result = dispatch.send_for_slot(1, "+15551234567", "hello", idempotency_key="http-err")
    assert result.success is False
    record = dispatch._outbox.get("http-err")
    assert record is not None
    assert record.status is OutboundMessageStatus.FAILED

    gateway = FakeGateway(
        [
            SmsSendResult(
                success=False,
                to_number="+15551234567",
                error="provider reported success=false",
                outcome=SmsSendOutcome.FAILURE,
            )
        ]
    )
    dispatch, _ = service(gateway=gateway)
    result = dispatch.send_for_slot(1, "+15551234567", "hello")
    assert result.success is False

    gateway = FakeGateway(
        [
            SmsSendResult(
                success=False,
                to_number="+15551234567",
                error="unknown: provider success without message id",
                outcome=SmsSendOutcome.UNKNOWN,
            )
        ]
    )
    dispatch, _ = service(gateway=gateway)
    result = dispatch.send_for_slot(1, "+15551234567", "hello", idempotency_key="no-id")
    assert result.outcome is SmsSendOutcome.UNKNOWN
    assert dispatch._outbox.get("no-id").status is OutboundMessageStatus.UNKNOWN


def test_timeout_unknown_is_not_retried():
    gateway = FakeGateway(
        [
            SmsSendResult(
                success=False,
                to_number="+15551234567",
                error="unknown: timeout after provider request",
                outcome=SmsSendOutcome.UNKNOWN,
            ),
            SmsSendResult(success=True, to_number="+15551234567", provider_message_id="late"),
        ]
    )
    dispatch, _ = service(gateway=gateway, retry_policy=SmsRetryPolicy(max_attempts=3))
    result = dispatch.send_for_slot(1, "+15551234567", "hello")
    assert result.outcome is SmsSendOutcome.UNKNOWN
    assert len(gateway.send_calls) == 1


def test_retryable_transport_error_retries_once():
    gateway = FakeGateway(
        [
            SmsSendResult(success=False, to_number="+15551234567", error="request_error: connection reset"),
            SmsSendResult(success=True, to_number="+15551234567", provider_message_id="m2"),
        ]
    )
    sleeps: list[float] = []
    dispatch, _ = service(
        gateway=gateway,
        retry_policy=SmsRetryPolicy(max_attempts=2),
        sleeper=sleeps.append,
    )
    result = dispatch.send_for_slot(1, "+15551234567", "hello")
    assert result.success is True
    assert len(gateway.send_calls) == 2
    assert sleeps == [0.5]


def test_concurrent_sends_on_one_slot():
    coordinator = SlotOperationCoordinator([1, 2])
    started = threading.Event()
    release = threading.Event()

    class BlockingGateway(FakeGateway):
        def send(self, to_number, message, device_ids, *, sim_slot=None):
            started.set()
            release.wait(1)
            return super().send(to_number, message, device_ids, sim_slot=sim_slot)

    gateway = BlockingGateway()
    dispatch, _ = service(gateway=gateway, coordinator=coordinator)
    results: list[SmsSendResult] = []

    def first():
        results.append(dispatch.send_for_slot(1, "+15551234567", "a", idempotency_key="a"))

    worker = threading.Thread(target=first)
    worker.start()
    assert started.wait(1)
    second = dispatch.send_for_slot(1, "+15551234567", "b", idempotency_key="b")
    release.set()
    worker.join(1)
    assert second.success is False
    assert "busy" in (second.error or "")
    assert results[0].success is True
    assert len(gateway.send_calls) == 1


def test_concurrent_sends_on_different_slots():
    coordinator = SlotOperationCoordinator([1, 2])
    dispatch, gateway = service(coordinator=coordinator)
    results: list[SmsSendResult] = []

    def run(slot_id: int, key: str):
        results.append(dispatch.send_for_slot(slot_id, "+15551234567", "x", idempotency_key=key))

    workers = [
        threading.Thread(target=run, args=(1, "s1")),
        threading.Thread(target=run, args=(2, "s2")),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(1)
    assert [item.success for item in results] == [True, True]
    assert len(gateway.send_calls) == 2
    used = {tuple(call[2]) for call in gateway.send_calls}
    assert used == {("215",), ("216",)}


def test_device_disconnect_during_operation_blocks_send():
    guard = SlotIdentityGuard({1: "SERIAL-01-ABCD"}, adb_states={"SERIAL-01-ABCD": "offline"})
    dispatch, gateway = service(identity_guard=guard, device_map={1: "215"})
    result = dispatch.send_for_slot(1, "+15551234567", "hello")
    assert result.success is False
    assert "offline" in (result.error or "")
    assert gateway.send_calls == []


def test_logs_are_redacted(caplog):
    dispatch, _ = service(dry_run=True)
    with caplog.at_level("INFO"):
        dispatch.send_for_slot(1, "+15551234567", "secret-body")
    assert "+15551234567" not in caplog.text
    assert "secret-body" not in caplog.text
    assert redact_phone("+15551234567") in caplog.text
    assert "1C101FDF6009EZ" not in caplog.text
    assert redact_serial("1C101FDF6009EZ") == "1C10****09EZ"


def test_production_prototype_isolation_blocks_prototype_serial():
    from domain.farm import ISOLATED_PROTOTYPE_SERIALS

    serial = next(iter(ISOLATED_PROTOTYPE_SERIALS))
    guard = SlotIdentityGuard({1: serial})
    with pytest.raises(Exception, match="prototype"):
        guard.verify(1)
