"""Tests for VoidFix inbound webhook listener body parsing and HTTP handler."""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_LISTENER_PATH = ROOT / "tools" / "voidfix_inbound_webhook_listener.py"
_spec = importlib.util.spec_from_file_location("voidfix_inbound_webhook_listener", _LISTENER_PATH)
assert _spec and _spec.loader
_listener = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_listener)

parse_inbound_http_body = _listener.parse_inbound_http_body
WebhookBodyError = _listener.WebhookBodyError
slot_for_voidfix_device = _listener.slot_for_voidfix_device
Handler = _listener.Handler
WEBHOOK_PATH = _listener.WEBHOOK_PATH

# Observed VoidFix received-message webhook (Slot 2 → Slot 1 test, 2026-09-21).
REAL_VOIDFIX_MESSAGE = [
    {
        "ID": 3956798,
        "number": "+16514722709",
        "message": "Mobi-Rent inbound webhook TEST slot2-to-slot1 run=20260921T213730Z",
        "deviceID": 1386,
        "simSlot": 1,
        "schedule": None,
        "userID": 671,
        "groupID": None,
        "status": "Received",
        "resultCode": None,
        "errorCode": None,
        "type": None,
        "attachments": None,
        "prioritize": None,
        "retries": None,
        "sentDate": "2026-09-21T21:37:33+0000",
        "deliveredDate": "2026-09-21T21:37:35+0000",
        "expiryDate": None,
        "locationId": "",
    }
]


class MockDispatchService:
    def __init__(self, *, webhook_secret: str | None = None) -> None:
        self.webhook_secret = webhook_secret

    def ingest_inbound(self, payload: object, provided_secret: str | None = None):
        if self.webhook_secret:
            if provided_secret != self.webhook_secret:
                raise ValueError("inbound webhook secret mismatch")
        from infrastructure.voidfix_api import parse_inbound_payload

        return parse_inbound_payload(payload)


def _start_server(service: MockDispatchService) -> tuple[ThreadingHTTPServer, threading.Thread, int]:
    Handler.dispatch_service = service
    Handler.capture_dir = None
    Handler.webhook_path = WEBHOOK_PATH
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, port


def _post(port: int, body: bytes, content_type: str, secret: str | None = None) -> tuple[int, dict]:
    url = f"http://127.0.0.1:{port}{WEBHOOK_PATH}"
    if secret:
        url += f"?secret={secret}"
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_parse_valid_json_webhook():
    body = {"messages": [{"number": "+1", "message": "hi", "deviceID": "1386"}]}
    raw = json.dumps(body).encode()
    assert parse_inbound_http_body(raw, "application/json") == body


def test_parse_valid_form_voidfix_webhook():
    raw = urllib.parse.urlencode(
        {"messages": json.dumps(REAL_VOIDFIX_MESSAGE)}
    ).encode()
    parsed = parse_inbound_http_body(raw, "application/x-www-form-urlencoded")
    assert parsed == REAL_VOIDFIX_MESSAGE


def test_parse_malformed_messages_json():
    raw = b"messages=not-json"
    with pytest.raises(WebhookBodyError, match="not valid JSON"):
        parse_inbound_http_body(raw, "application/x-www-form-urlencoded")


def test_parse_missing_messages_field():
    raw = b"other=1"
    with pytest.raises(WebhookBodyError, match="missing messages"):
        parse_inbound_http_body(raw, "application/x-www-form-urlencoded")


def test_http_json_webhook_200():
    server, _thread, port = _start_server(MockDispatchService())
    try:
        status, data = _post(
            port,
            json.dumps({"number": "+1", "message": "x", "deviceID": "1386"}).encode(),
            "application/json",
        )
        assert status == 200
        assert data["ok"] is True
        assert data["parsed_count"] == 1
    finally:
        server.shutdown()


def test_http_form_voidfix_webhook_200():
    server, _thread, port = _start_server(MockDispatchService())
    try:
        body = urllib.parse.urlencode({"messages": json.dumps(REAL_VOIDFIX_MESSAGE)}).encode()
        status, data = _post(port, body, "application/x-www-form-urlencoded")
        assert status == 200
        assert data["ok"] is True
        assert data["device_ids"] == ["1386"]
    finally:
        server.shutdown()


def test_http_invalid_webhook_secret_401():
    server, _thread, port = _start_server(MockDispatchService(webhook_secret="expected"))
    try:
        status, data = _post(
            port,
            json.dumps({"number": "+1", "message": "x"}).encode(),
            "application/json",
            secret="wrong",
        )
        assert status == 401
        assert "secret" in data["error"].lower()
    finally:
        server.shutdown()


def test_slot_mapping_device_1386_to_slot_1():
    device_map = {1: "1386", 2: "1389"}
    assert slot_for_voidfix_device("1386", device_map) == 1
    assert slot_for_voidfix_device("9999", device_map) is None


def test_real_payload_fields_after_ingest():
    service = MockDispatchService()
    messages = service.ingest_inbound(REAL_VOIDFIX_MESSAGE)
    assert len(messages) == 1
    assert messages[0].from_number == "+16514722709"
    assert messages[0].device_id == "1386"
    assert "slot2-to-slot1" in messages[0].message
    device_map = {1: "1386", 2: "1389"}
    assert slot_for_voidfix_device(messages[0].device_id, device_map) == 1
