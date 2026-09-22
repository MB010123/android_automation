from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.farm import SmsFinalStatus, VoidFixDeliveryPollPolicy
from infrastructure.voidfix_delivery import (
    VoidFixDeliveryPoller,
    classify_delivery,
    find_message_by_id,
    is_delivered,
    is_delivery_failed,
    snapshot_from_row,
)

MESSAGE_3933486 = {
    "ID": 3933486,
    "number": "+19522287088",
    "message": "Mobi-Rent Slot 1 SMS TEST",
    "deviceID": 1386,
    "simSlot": 1,
    "status": "Sent",
    "resultCode": -1,
    "errorCode": -1,
    "sentDate": "2026-09-18T19:06:48+0000",
    "deliveredDate": "2026-09-18T19:06:53+0000",
}


def test_delivered_date_authoritative_over_sent_status():
    assert is_delivered(MESSAGE_3933486) is True
    assert classify_delivery(MESSAGE_3933486) is SmsFinalStatus.DELIVERED


def test_failed_status_and_error_code():
    row = dict(MESSAGE_3933486)
    row["deliveredDate"] = None
    row["status"] = "Failed"
    assert is_delivery_failed(row) is True
    assert classify_delivery(row) is SmsFinalStatus.FAILED


def test_sent_without_delivered_date_keeps_polling_class():
    row = dict(MESSAGE_3933486)
    row["deliveredDate"] = None
    row["status"] = "Sent"
    assert is_delivered(row) is False
    assert is_delivery_failed(row) is False
    assert classify_delivery(row) is SmsFinalStatus.SENT


def test_find_message_by_id_in_read_messages_payload():
    payload = {"success": True, "data": {"messages": [MESSAGE_3933486, {"ID": 1}]}}
    found = find_message_by_id(payload, "3933486")
    assert found is not None
    assert found["ID"] == 3933486


def test_poll_timeout_marks_unknown(monkeypatch):
    class FakeSession:
        def post(self, *args, **kwargs):
            class Resp:
                status_code = 200

                def json(self):
                    return {"success": True, "data": {"messages": []}}

            return Resp()

    poller = VoidFixDeliveryPoller("secret-key", session=FakeSession())
    times = iter([0.0, 0.0, 200.0])
    result = poller.poll_until_terminal(
        "999",
        policy=VoidFixDeliveryPollPolicy(interval_seconds=1.0, timeout_seconds=10.0),
        sleeper=lambda _s: None,
        monotonic=lambda: next(times),
    )
    assert result.final_status is SmsFinalStatus.TIMEOUT
    assert result.timed_out is True


def test_snapshot_fields():
    snap = snapshot_from_row(MESSAGE_3933486)
    assert snap.message_id == "3933486"
    assert snap.device_id == "1386"
    assert snap.sim_slot == 1
    assert snap.delivered_date.startswith("2026-09-18")


@pytest.mark.skipif(
    os.getenv("VOIDFIX_LIVE_READ_MESSAGES") != "1",
    reason="set VOIDFIX_LIVE_READ_MESSAGES=1 to query live read-messages.php",
)
def test_live_read_messages_finds_3933486():
    from dotenv import dotenv_values

    key = (dotenv_values("config/prototype/prototype.env").get("VOIDFIX_API_KEY") or "").strip()
    if not key:
        pytest.skip("VOIDFIX_API_KEY missing")
    poller = VoidFixDeliveryPoller(key)
    row = poller.find_message("3933486")
    assert row is not None
    assert is_delivered(row)
    assert str(row.get("ID")) == "3933486"
