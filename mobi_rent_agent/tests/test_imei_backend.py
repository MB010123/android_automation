from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.models import SlotDeviceRecord
from infrastructure.imei_backend import (
    IMEI2_DDL,
    HttpImei2Backend,
    Imei2BackendError,
    _parse_queue_row,
)

DOC_IMEI = "490154203237518"
DOC_IMEI_B = "356938035643809"
SLOT1_UUID = "9423e3cf-19bd-434a-994d-445941d58f48"
OTHER_UUID = "151d4340-d7d0-4330-a59d-bff0a71ac47b"


class FakeResponse:
    def __init__(self, body, status_code: int = 200, text: str | None = None) -> None:
        self._body = body
        self.status_code = status_code
        self.text = text if text is not None else ""

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, get_bodies: list, post_status: int = 200, post_text: str = "{}") -> None:
        self._get_bodies = list(get_bodies)
        self._post_status = post_status
        self._post_text = post_text
        self.headers: dict[str, str] = {}
        self.gets: list[tuple[str, dict]] = []
        self.posts: list[tuple[str, dict]] = []

    def get(self, endpoint: str, **kwargs):
        self.gets.append((endpoint, kwargs))
        body = self._get_bodies.pop(0)
        if isinstance(body, FakeResponse):
            return body
        return FakeResponse(body)

    def post(self, endpoint: str, **kwargs):
        self.posts.append((endpoint, kwargs))
        return FakeResponse({}, status_code=self._post_status, text=self._post_text)


def _queue(imei2=None):
    return [
        {
            "slot_id": OTHER_UUID,
            "motherboard_slot_num": None,
            "hardware_box_id": None,
            "status": "pending_esim",
        },
        {
            "slot_id": SLOT1_UUID,
            "motherboard_slot_num": 1,
            "hardware_box_id": "POD_01",
            "status": "networkerror",
            **({"imei2": imei2} if imei2 is not None else {}),
        },
    ]


def make_client(session: FakeSession) -> HttpImei2Backend:
    client = HttpImei2Backend("https://mobi-rent.example/api/public/hardware/queue", "tok")
    client._session = session
    return client


def test_us_mobile_device_info_omits_imei1():
    record = SlotDeviceRecord(slot_id=1, imei2=DOC_IMEI, imei1=DOC_IMEI_B)
    info = record.us_mobile_device_info()
    assert info["imei2"] == DOC_IMEI
    assert info["label"] == "IMEI 2 / Digital IMEI"
    assert "imei1" not in info


def test_lookup_uses_motherboard_slot_num_not_uuid():
    session = FakeSession([_queue(DOC_IMEI)])
    client = make_client(session)
    record = client.slot_for_bay(1)
    assert record is not None
    assert record.record_id == SLOT1_UUID
    assert record.imei2 == DOC_IMEI


def test_rental_assigned_slot_returns_imei2_only():
    session = FakeSession([_queue(DOC_IMEI)])
    assert make_client(session).imei2_for_assigned_slot(1) == DOC_IMEI


def test_register_posts_only_slot1_imei2(caplog):
    session = FakeSession([_queue(), _queue(DOC_IMEI), _queue(DOC_IMEI)])
    client = make_client(session)
    with caplog.at_level(logging.INFO):
        result = client.register_imei2(1, DOC_IMEI, adb_serial="1C101FDF6009EZ")
    assert result.persisted is True
    assert result.record_id == SLOT1_UUID
    posted = session.posts[0][1]["json"]
    assert posted == {
        "hardware_agent_token": "tok",
        "slot_id": 1,
        "status": "networkerror",
        "imei2": DOC_IMEI,
    }
    assert "imei1" not in posted
    assert "adb_serial" not in posted
    assert DOC_IMEI not in caplog.text
    assert "4901…7518" in caplog.text


def test_refuses_missing_backend_row():
    session = FakeSession([[{"slot_id": OTHER_UUID, "motherboard_slot_num": None, "status": "pending_esim"}]])
    client = make_client(session)
    with pytest.raises(Imei2BackendError, match="no backend slots row"):
        client.register_imei2(1, DOC_IMEI)


def test_refuses_slots_2_20():
    session = FakeSession([])
    client = make_client(session)
    with pytest.raises(Imei2BackendError, match="slots 2-20"):
        client.register_imei2(2, DOC_IMEI)


def test_missing_column_error_includes_ddl():
    session = FakeSession(
        [_queue()],
        post_status=400,
        post_text='{"code":"42703","message":"column slots.imei2 does not exist"}',
    )
    client = make_client(session)
    with pytest.raises(Imei2BackendError, match="imei2 column") as exc:
        client.register_imei2(1, DOC_IMEI)
    assert IMEI2_DDL in str(exc.value)


def test_detects_post_ignored_without_persisting():
    session = FakeSession([_queue(), _queue(), _queue()])
    client = make_client(session)
    with pytest.raises(Imei2BackendError, match="did not return imei2"):
        client.register_imei2(1, DOC_IMEI)


def test_parse_queue_row_ignores_imei1():
    row = _parse_queue_row(
        {
            "slot_id": SLOT1_UUID,
            "motherboard_slot_num": 1,
            "imei1": DOC_IMEI_B,
            "imei2": DOC_IMEI,
        },
        0,
    )
    assert row.imei2 == DOC_IMEI
