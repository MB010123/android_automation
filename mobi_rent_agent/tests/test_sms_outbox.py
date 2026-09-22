from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from application.sms_outbox import DuplicateIdempotencyError, SmsOutbox
from domain.farm import OutboundMessageStatus


def test_reserve_and_duplicate_key():
    outbox = SmsOutbox()
    first = outbox.reserve(slot_id=1, to_number="+15551234567", idempotency_key="k1")
    assert first.status is OutboundMessageStatus.PENDING
    assert "1555" not in first.to_number_redacted or first.to_number_redacted.startswith("+XXX")
    with pytest.raises(DuplicateIdempotencyError):
        outbox.reserve(slot_id=1, to_number="+15551234567", idempotency_key="k1")


def test_update_status_and_provider_id():
    outbox = SmsOutbox()
    outbox.reserve(slot_id=2, to_number="+15550001111", idempotency_key="k2")
    updated = outbox.update(
        "k2",
        status=OutboundMessageStatus.CONFIRMED,
        provider_message_id="3862679",
    )
    assert updated.provider_message_id == "3862679"
    assert outbox.get("k2").status is OutboundMessageStatus.CONFIRMED
