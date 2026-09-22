"""VPS backend and farm status HTTP (no SMS, no live farm)."""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infrastructure.inbound_message_store import InboundMessageStore
from infrastructure.voidfix_webhook_http import parse_inbound_http_body
from tools.farm_agent_status_server import Handler as FarmHandler, build_farm_status


def _load_vps_handler():
    path = ROOT / "tools" / "vps_backend_server.py"
    spec = importlib.util.spec_from_file_location("vps_backend_server", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Handler


VpsHandler = _load_vps_handler()


def test_farm_status_counts_online():
    slot_map = {1: "SER-A", 2: "SER-B"}
    body = build_farm_status(
        adb_path="adb-missing-for-test",
        slot_map=slot_map,
    )
    assert body["role"] == "farm"
    assert body["slot_count"] == 2


def test_farm_status_server_requires_auth():
    FarmHandler.api_token = "secret"
    FarmHandler.adb_path = "adb"
    FarmHandler.slot_map = {1: "X"}
    server = ThreadingHTTPServer(("127.0.0.1", 0), FarmHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(f"http://127.0.0.1:{port}/agent/health", method="GET")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=3)
        assert exc.value.code == 401
    finally:
        server.shutdown()


def test_vps_webhook_stores_inbound(tmp_path: Path):
    db = tmp_path / "inbound.sqlite"
    store = InboundMessageStore(db)
    VpsHandler.inbound_store = store
    VpsHandler.webhook_secret = None
    VpsHandler.webhook_path = "/voidfix/inbound"
    VpsHandler.device_map = {1: "1386"}
    VpsHandler.farm_agent_url = None
    VpsHandler.farm_agent_token = None
    VpsHandler.app_name = "test"

    server = ThreadingHTTPServer(("127.0.0.1", 0), VpsHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import urllib.request

        msg = [{"number": "+15551234567", "message": "hi", "deviceID": 1386}]
        import urllib.parse

        form = "messages=" + urllib.parse.quote(json.dumps(msg))
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
    finally:
        server.shutdown()
        store.close()


def test_parse_form_voidfix_body():
    payload = [{"deviceID": 1, "number": "+1", "message": "x"}]
    raw = ("messages=" + json.dumps(payload)).encode()
    parsed = parse_inbound_http_body(raw, "application/x-www-form-urlencoded")
    assert isinstance(parsed, list)
