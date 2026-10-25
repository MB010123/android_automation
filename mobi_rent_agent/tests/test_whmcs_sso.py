"""WHMCS dashboard SSO: HMAC over raw Base64URL payload; 10s nonce replay."""
from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.vps_lovable_routes import parse_route
from application.whmcs_sso import (
    WhmcsSsoService,
    b64url_decode,
    sign_sso_payload,
    verify_sso_signature,
)

SECRET = "test-sso-secret-not-production-32bytes"
USER = "11111111-1111-4111-8111-111111111111"


def _b64url(raw: dict) -> str:
    blob = json.dumps(raw, separators=(",", ":"), ensure_ascii=True)
    return __import__("base64").urlsafe_b64encode(blob.encode()).decode().rstrip("=")


def _token(nonce: str = "nonce-aaa11122", *, exp: int | None = None) -> tuple[str, str]:
    now = 1_700_000_000
    payload = _b64url(
        {
            "client_id": 41,
            "email": "renter@example.com",
            "user_id": USER,
            "nonce": nonce,
            "iat": now,
            "exp": exp if exp is not None else now + 60,
        }
    )
    return payload, sign_sso_payload(SECRET, payload)


def test_route_parses():
    parsed = parse_route("/api/public/auth/whmcs-sso")
    assert parsed is not None and parsed.kind == "whmcs_sso"


def test_hmac_is_over_raw_payload_not_json():
    payload, sig = _token()
    decoded = b64url_decode(payload)
    json_mac = hmac.new(SECRET.encode(), decoded, hashlib.sha256).hexdigest()
    assert sig != json_mac
    assert verify_sso_signature(SECRET, payload, sig) is True
    assert verify_sso_signature(SECRET, payload, json_mac) is False


def test_valid_token_returns_200():
    clock = {"t": 1_700_000_000.0}
    svc = WhmcsSsoService(secret=SECRET, clock=lambda: clock["t"])
    payload, sig = _token()
    result = svc.redeem(payload_b64=payload, signature=sig, ip="1.1.1.1", user_agent="jest")
    assert result.http_status == 200
    assert result.body["ok"] is True
    assert result.body["replayed"] is False
    assert result.body["user_id"] == USER
    assert result.body["whmcs_client_id"] == 41
    assert result.body["phone_count"] == 1


def test_react_strictmode_replay_same_fingerprint_is_200():
    clock = {"t": 1_700_000_000.0}
    svc = WhmcsSsoService(secret=SECRET, clock=lambda: clock["t"])
    payload, sig = _token("nonce-replay-1")
    first = svc.redeem(payload_b64=payload, signature=sig, ip="10.0.0.8", user_agent="Mozilla")
    second = svc.redeem(payload_b64=payload, signature=sig, ip="10.0.0.8", user_agent="Mozilla")
    assert first.http_status == 200
    assert second.http_status == 200
    assert second.body["replayed"] is True
    clock["t"] += 11.0
    late = svc.redeem(payload_b64=payload, signature=sig, ip="10.0.0.8", user_agent="Mozilla")
    assert late.http_status == 401


def test_same_nonce_different_ua_is_401():
    svc = WhmcsSsoService(secret=SECRET, clock=lambda: 1_700_000_000.0)
    payload, sig = _token("nonce-other-ua")
    assert svc.redeem(payload_b64=payload, signature=sig, ip="1.1.1.1", user_agent="A").http_status == 200
    denied = svc.redeem(payload_b64=payload, signature=sig, ip="1.1.1.1", user_agent="B")
    assert denied.http_status == 401
    assert denied.body["error"] == "unauthorized"


def test_bad_signature_or_missing_secret():
    payload, sig = _token()
    empty = WhmcsSsoService(secret="")
    assert empty.redeem(payload_b64=payload, signature=sig).http_status == 503
    svc = WhmcsSsoService(secret=SECRET, clock=time.time)
    assert svc.redeem(payload_b64=payload, signature="00" * 32).http_status == 401
    expired_payload, expired_sig = _token("nonce-expired", exp=1)
    frozen = WhmcsSsoService(secret=SECRET, clock=lambda: 1_700_000_000.0)
    assert frozen.redeem(payload_b64=expired_payload, signature=expired_sig).http_status == 401
