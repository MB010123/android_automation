"""Webhook inbound → outbound Farm dispatch (tests A–E)."""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.webhook_outbound_dispatcher import WebhookOutboundDispatcher
from application.webhook_reply_policy import WebhookReplyPolicy, WebhookReplyRule
from domain.models import InboundSms
from infrastructure.farm_sms_client import FarmDispatchResponse
from infrastructure.inbound_message_store import InboundMessageStore
from infrastructure.outbound_job_store import OutboundJobStore
from infrastructure.voidfix_api import parse_inbound_payload
from tools.farm_agent_status_server import Handler as FarmHandler, SMS_SEND_PATH


def _load_vps_handler():
    path = ROOT / "tools" / "vps_backend_server.py"
    spec = importlib.util.spec_from_file_location("vps_backend_server", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Handler


VpsHandler = _load_vps_handler()

REAL_VOIDFIX_MESSAGE = [
    {
        "ID": 9000001,
        "number": "+16514722709",
        "message": "webhook dispatch test",
        "deviceID": 1386,
        "status": "Received",
    }
]


@dataclass
class MockFarmClient:
    calls: int = 0
    fail: bool = False
    unreachable: bool = False

    def send_sms(self, **kwargs: object) -> FarmDispatchResponse:
        self.calls += 1
        if self.unreachable:
            return FarmDispatchResponse(ok=False, http_status=0, body={}, error="timeout")
        if self.fail:
            return FarmDispatchResponse(ok=False, http_status=502, body={}, error="bad gateway")
        return FarmDispatchResponse(
            ok=True,
            http_status=200,
            body={
                "ok": True,
                "status": "sent",
                "provider_message_id": "3970000",
            },
        )


def _policy_enabled_slot2_to_slot1() -> WebhookReplyPolicy:
    return WebhookReplyPolicy(
        enabled=True,
        rules=(WebhookReplyRule(inbound_slot=1, sender_slot=1, reply_to_slot=2),),
    )


def test_a_webhook_parsed_mapped_stored(tmp_path: Path):
    db = tmp_path / "inbound.sqlite"
    store = InboundMessageStore(db)
    VpsHandler.inbound_store = store
    VpsHandler.outbound_dispatcher = None
    VpsHandler.webhook_secret = None
    VpsHandler.webhook_path = "/voidfix/inbound"
    VpsHandler.device_map = {1: "1386"}
    VpsHandler.app_name = "test"

    server = ThreadingHTTPServer(("127.0.0.1", 0), VpsHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import urllib.parse
        import urllib.request

        form = "messages=" + urllib.parse.quote(json.dumps(REAL_VOIDFIX_MESSAGE))
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/voidfix/inbound",
            data=form.encode(),
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            body = json.loads(resp.read().decode())
        assert body["ok"] is True
        assert body["mapped_slots"] == [1]
        assert store.count() == 1
        messages = parse_inbound_payload(REAL_VOIDFIX_MESSAGE)
        assert messages[0].device_id == "1386"
    finally:
        server.shutdown()
        store.close()


def test_b_webhook_dispatches_to_farm(tmp_path: Path):
    jobs = OutboundJobStore(tmp_path / "jobs.sqlite")
    farm = MockFarmClient()
    dispatcher = WebhookOutboundDispatcher(
        job_store=jobs,
        farm_client=farm,
        policy=_policy_enabled_slot2_to_slot1(),
        max_dispatch_attempts=1,
    )
    VpsHandler.inbound_store = InboundMessageStore(tmp_path / "inbound.sqlite")
    VpsHandler.outbound_dispatcher = dispatcher
    VpsHandler.webhook_secret = None
    VpsHandler.webhook_path = "/voidfix/inbound"
    VpsHandler.device_map = {1: "1386"}
    VpsHandler.app_name = "test"

    server = ThreadingHTTPServer(("127.0.0.1", 0), VpsHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import urllib.parse
        import urllib.request

        form = "messages=" + urllib.parse.quote(json.dumps(REAL_VOIDFIX_MESSAGE))
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/voidfix/inbound",
            data=form.encode(),
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            body = json.loads(resp.read().decode())
        assert body["dispatch"][0]["dispatched"] is True
        assert farm.calls == 1
        assert jobs.get_by_inbound_event("voidfix:9000001") is not None
    finally:
        server.shutdown()
        jobs.close()


def test_c_duplicate_webhook_one_dispatch(tmp_path: Path):
    jobs = OutboundJobStore(tmp_path / "jobs.sqlite")
    farm = MockFarmClient()
    dispatcher = WebhookOutboundDispatcher(
        job_store=jobs,
        farm_client=farm,
        policy=_policy_enabled_slot2_to_slot1(),
        max_dispatch_attempts=1,
    )
    msg = parse_inbound_payload(REAL_VOIDFIX_MESSAGE)[0]
    first = dispatcher.process_inbound(
        msg,
        inbound_row_id=1,
        inbound_slot=1,
        payload=REAL_VOIDFIX_MESSAGE,
        index=0,
    )
    second = dispatcher.process_inbound(
        msg,
        inbound_row_id=2,
        inbound_slot=1,
        payload=REAL_VOIDFIX_MESSAGE,
        index=0,
    )
    assert first["dispatched"] is True
    assert second.get("duplicate") is True
    assert farm.calls == 1
    assert len({first["outbound_job_id"], second["outbound_job_id"]}) == 1


def test_d_farm_unavailable_job_failed(tmp_path: Path):
    jobs = OutboundJobStore(tmp_path / "jobs.sqlite")
    farm = MockFarmClient(unreachable=True)
    dispatcher = WebhookOutboundDispatcher(
        job_store=jobs,
        farm_client=farm,
        policy=_policy_enabled_slot2_to_slot1(),
        max_dispatch_attempts=1,
    )
    msg = parse_inbound_payload(REAL_VOIDFIX_MESSAGE)[0]
    result = dispatcher.process_inbound(
        msg,
        inbound_row_id=1,
        inbound_slot=1,
        payload=REAL_VOIDFIX_MESSAGE,
        index=0,
    )
    assert result["dispatched"] is False
    assert result["status"] == "failed"
    record = jobs.get_by_inbound_event("voidfix:9000001")
    assert record is not None
    assert record.status == "failed"
    assert record.error


def test_e_farm_sms_send_requires_auth():
    FarmHandler.api_token = "secret-token"
    FarmHandler.adb_path = "adb"
    FarmHandler.slot_map = {1: "X"}
    FarmHandler.agent_config = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), FarmHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{SMS_SEND_PATH}",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=3)
        assert exc.value.code == 401
    finally:
        server.shutdown()
