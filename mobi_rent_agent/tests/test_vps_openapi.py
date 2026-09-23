"""OpenAPI / Swagger UI for VPS backend (no secrets in spec)."""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infrastructure.vps_api_docs import OPENAPI_JSON_PATH, REDOC_PATH, SWAGGER_UI_PATH
from infrastructure.vps_openapi_spec import OPENAPI_VERSION, build_vps_openapi_document

SECRET_PATTERNS = [
    re.compile(r"sk_[a-zA-Z0-9]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}"),
    re.compile(r"FARM_SERVICE_TOKEN\s*=\s*\S+"),
    re.compile(r"FARM_AGENT_API_TOKEN\s*=\s*\S+"),
]

REQUIRED_PATHS = [
    "/health",
    "/farm/status",
    "/farm/slots/available",
    "/farm/slots/{bay}/assign",
    "/jobs/{job_id}",
    f"/slots/{{slot_id}}/sms/send",
    "/messages/{message_id}",
    f"/slots/{{slot_id}}/messages",
    f"/slots/{{slot_id}}/actions/{{action}}",
    f"/slots/{{slot_id}}/events",
    "/voidfix/inbound",
]


def test_openapi_document_structure():
    doc = build_vps_openapi_document(voidfix_webhook_path="/voidfix/inbound")
    assert doc["openapi"] == OPENAPI_VERSION
    assert doc["openapi"].startswith("3.")
    assert "paths" in doc
    assert "components" in doc
    schemes = doc["components"]["securitySchemes"]
    assert "FarmServiceBearer" in schemes
    assert schemes["FarmServiceBearer"]["scheme"] == "bearer"


def test_required_paths_and_security():
    doc = build_vps_openapi_document()
    paths = doc["paths"]
    for p in REQUIRED_PATHS:
        assert p in paths, f"missing path {p}"
    assign = paths["/farm/slots/{bay}/assign"]["post"]
    assert assign.get("security") == [{"FarmServiceBearer": []}]
    health = paths["/health"]["get"]
    assert "security" not in health


def test_schemas_present():
    doc = build_vps_openapi_document()
    schemas = doc["components"]["schemas"]
    for name in (
        "SmsSendRequest",
        "SmsMessageResponse",
        "JobResponse",
        "AssignmentRequest",
        "ActionRequest",
        "ErrorResponse",
        "LovableInboundNormalizedPayload",
    ):
        assert name in schemas


def test_unsupported_actions_documented():
    doc = build_vps_openapi_document()
    action = doc["paths"]["/slots/{slot_id}/actions/{action}"]["post"]
    param = next(p for p in action["parameters"] if p["name"] == "action")
    assert "airplane_cycle" in param["schema"]["enum"]
    desc = param.get("description") or ""
    assert "action_not_supported" in desc


def test_no_secrets_in_openapi_json():
    doc = build_vps_openapi_document()
    raw = json.dumps(doc)
    assert "<FARM_SERVICE_TOKEN>" in raw or "FARM_SERVICE_TOKEN" in raw
    for pattern in SECRET_PATTERNS:
        assert not pattern.search(raw), pattern.pattern
    # Env var names as placeholders are OK; reject only real-looking token values.
    assert "FARM_AGENT_API_TOKEN=" not in raw
    assert "FARM_SERVICE_TOKEN=" not in raw


def _start_docs_server():
    path = ROOT / "tools" / "vps_backend_server.py"
    spec = importlib.util.spec_from_file_location("vps_backend_server", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    Handler = mod.Handler
    Handler.webhook_path = "/voidfix/inbound"
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def test_http_openapi_and_swagger_endpoints():
    server, port = _start_docs_server()
    try:
        base = f"http://127.0.0.1:{port}"
        with urllib.request.urlopen(f"{base}{OPENAPI_JSON_PATH}", timeout=3) as resp:
            assert resp.status == 200
            body = json.loads(resp.read().decode())
        assert body["openapi"].startswith("3.")
        with urllib.request.urlopen(f"{base}{SWAGGER_UI_PATH}", timeout=3) as resp:
            html = resp.read().decode()
        assert "swagger-ui" in html.lower()
        with urllib.request.urlopen(f"{base}{REDOC_PATH}", timeout=3) as resp:
            redoc = resp.read().decode()
        assert "redoc" in redoc.lower()
    finally:
        server.shutdown()
