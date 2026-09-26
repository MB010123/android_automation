"""Auth route parsing and farm-service session (not a user login)."""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.vps_lovable_auth import drop_sensitive_fields
from application.vps_lovable_routes import parse_route
from infrastructure.slot_public_id import public_id_for_farm_slot

SLOT1 = public_id_for_farm_slot(1)


def test_drop_sensitive_fields_never_keeps_password():
    cleaned = drop_sensitive_fields(
        {"email": "user@example.com", "password": "should-not-leak", "Password": "also"}
    )
    dumped = json.dumps(cleaned).lower()
    assert "should-not-leak" not in dumped
    assert "password" not in dumped
    assert cleaned.get("email") == "user@example.com"


def test_route_auth_and_slots():
    assert parse_route("/auth/signup").kind == "auth_signup"
    assert parse_route("/auth/login").kind == "auth_login"
    assert parse_route("/auth/logout").kind == "auth_logout"
    assert parse_route("/auth/me").kind == "auth_me"
    session = parse_route("/auth/session")
    assert session is not None and session.kind == "auth_session"
    listed = parse_route("/slots")
    assert listed is not None and listed.kind == "slots_list"
    detail = parse_route(f"/slots/{SLOT1}")
    assert detail is not None and detail.kind == "slot_detail"
    esim = parse_route(f"/slots/{SLOT1}/esim")
    assert esim is not None and esim.kind == "slot_esim"
    assert parse_route(f"/slots/{SLOT1}/status").kind == "slot_status"


def _load_handler():
    path = ROOT / "tools" / "vps_backend_server.py"
    spec = importlib.util.spec_from_file_location("vps_backend_server_auth", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod.Handler


def test_http_session_requires_farm_token():
    Handler = _load_handler()
    Handler.farm_service_token = "service-secret"
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{port}/auth/session"
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(urllib.request.Request(url), timeout=3)
        assert exc.value.code == 401
        req = urllib.request.Request(url, headers={"Authorization": "Bearer service-secret"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            body = json.loads(resp.read().decode())
        assert resp.status == 200
        assert body["audience"] == "farm_service"
        assert body["token_type"] == "FARM_SERVICE_TOKEN"
    finally:
        server.shutdown()
