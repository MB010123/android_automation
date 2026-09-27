"""Supabase Auth + tenant table wiring (mocked HTTP; no live project)."""
from __future__ import annotations

from typing import Any

from application.auth_service import AuthService
from infrastructure.supabase_gateway import SupabaseGateway
from infrastructure.supabase_tenant_store import SupabaseTenantStore


STRONG = "correct-horse-battery-staple"


class FakeResponse:
    def __init__(self, status: int, payload: Any = None) -> None:
        self.status_code = status
        self._payload = payload
        self.content = b"{}" if payload is not None else b""

    def json(self) -> Any:
        return self._payload


class FakeHTTP:
    def __init__(self, routes: dict[tuple[str, str], FakeResponse]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def request(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append((method, url, json))
        key = (method.upper(), url)
        if key in self.routes:
            return self.routes[key]
        for (m, prefix), resp in self.routes.items():
            if method.upper() == m and url.startswith(prefix):
                return resp
        return FakeResponse(404, {"error": "not_found"})

    def close(self) -> None:
        return None


USER = {
    "id": "11111111-1111-4111-8111-111111111111",
    "email": "owner@example.com",
    "email_confirmed_at": "2026-01-01T00:00:00Z",
    "created_at": "2026-01-01T00:00:00Z",
    "last_sign_in_at": "2026-01-02T00:00:00Z",
}

TOKEN_BODY = {
    "access_token": "supabase-access-token",
    "refresh_token": "supabase-refresh",
    "expires_in": 3600,
    "token_type": "bearer",
    "user": USER,
}


def _gateway(routes: dict[tuple[str, str], FakeResponse]) -> tuple[SupabaseGateway, FakeHTTP]:
    http = FakeHTTP(routes)
    gw = SupabaseGateway(
        "https://example.supabase.co",
        "anon-test-key",
        service_role_key="service-test-key",
        session=http,  # type: ignore[arg-type]
    )
    return gw, http


def test_signup_login_and_validate_use_gotrue():
    gw, http = _gateway(
        {
            ("POST", "https://example.supabase.co/auth/v1/signup"): FakeResponse(200, TOKEN_BODY),
            ("POST", "https://example.supabase.co/rest/v1/profiles"): FakeResponse(201, []),
            ("POST", "https://example.supabase.co/auth/v1/token?grant_type=password"): FakeResponse(200, TOKEN_BODY),
            ("GET", "https://example.supabase.co/auth/v1/user"): FakeResponse(200, USER),
            ("POST", "https://example.supabase.co/auth/v1/logout"): FakeResponse(204, {}),
        }
    )
    tenant = SupabaseTenantStore(gw)
    svc = AuthService(supabase=gw, tenant=tenant)
    created = svc.signup({"email": "Owner@example.com", "password": STRONG})
    assert created.http_status == 200
    assert created.body["user"]["id"] == USER["id"]
    assert created.body["session"]["access_token"] == "supabase-access-token"
    assert created.access_token == "supabase-access-token"
    assert created.body["user"].get("password") is None
    assert any("/auth/v1/signup" in url for _, url, _ in http.calls)
    assert any("/rest/v1/profiles" in url for _, url, _ in http.calls)

    logged = svc.login({"email": "owner@example.com", "password": STRONG})
    assert logged.http_status == 200
    ctx = svc.validate_user_token("supabase-access-token")
    assert ctx is not None
    assert ctx.user is not None
    assert ctx.user.user_id == USER["id"]
    assert ctx.user.password_hash == ""
    out = svc.logout(ctx)
    assert out.http_status == 200


def test_login_invalid_credentials_are_generic():
    gw, _http = _gateway(
        {
            ("POST", "https://example.supabase.co/auth/v1/token?grant_type=password"): FakeResponse(
                400, {"error_description": "Invalid login credentials"}
            ),
        }
    )
    svc = AuthService(supabase=gw)
    result = svc.login({"email": "owner@example.com", "password": STRONG})
    assert result.http_status == 401
    assert result.body["error"] == "invalid_credentials"
    assert "Invalid login" not in str(result.body)


def test_signup_duplicate_is_generic():
    gw, _http = _gateway(
        {
            ("POST", "https://example.supabase.co/auth/v1/signup"): FakeResponse(
                422, {"msg": "User already registered"}
            ),
        }
    )
    svc = AuthService(supabase=gw)
    result = svc.signup({"email": "owner@example.com", "password": STRONG})
    assert result.http_status == 400
    assert result.body["error"] == "invalid_request"


def test_forgot_password_always_ok_and_calls_recover():
    gw, http = _gateway(
        {
            ("POST", "https://example.supabase.co/auth/v1/recover"): FakeResponse(200, {}),
        }
    )
    svc = AuthService(supabase=gw)
    result = svc.forgot_password({"email": "owner@example.com"})
    assert result.http_status == 200
    assert result.body == {"ok": True}
    assert any("/auth/v1/recover" in url for _, url, _ in http.calls)


def test_tenant_ownership_claim_and_esim():
    slot_row = {
        "id": "22222222-2222-4222-8222-222222222222",
        "user_id": None,
        "motherboard_slot_num": 3,
        "carrier_name": "Tello",
        "phone_number": "+15555550123",
        "imei2": "123456789012345",
        "hardware_box_id": "POD_01",
    }
    gw, http = _gateway(
        {
            ("GET", "https://example.supabase.co/rest/v1/slots"): FakeResponse(200, [slot_row]),
            ("PATCH", "https://example.supabase.co/rest/v1/slots"): FakeResponse(
                200, [{**slot_row, "user_id": USER["id"]}]
            ),
            ("POST", "https://example.supabase.co/rest/v1/esim_uploads"): FakeResponse(
                201, [{"id": "esim-1", "slot_id": slot_row["id"], "qr_code_url": "esim-records/a.png"}]
            ),
            ("POST", "https://example.supabase.co/rest/v1/messages"): FakeResponse(201, [{"id": "msg-1"}]),
        }
    )
    tenant = SupabaseTenantStore(gw)
    assert tenant.owner_of_slot(3) is None
    assert tenant.claim_slot(3, USER["id"], "00000000-0000-4000-8000-000000000099") is True
    esim_id = tenant.record_esim_upload(
        user_id=USER["id"],
        farm_slot_id=3,
        storage_key="esim-records/a.png",
        rental_id=None,
        carrier="Tello",
        job_id="job-1",
    )
    assert esim_id == "esim-1"
    tenant.record_message(
        farm_slot_id=3,
        message_id="m-1",
        direction="outbound",
        phone_number="+15555550999",
        message_body="hello",
        status="queued",
    )
    assert any("/rest/v1/esim_uploads" in url for _, url, _ in http.calls)
    assert any("/rest/v1/messages" in url for _, url, _ in http.calls)
    patch_bodies = [body for method, url, body in http.calls if method == "PATCH" and "/slots" in url]
    assert patch_bodies
    assert "proxy_auth" not in patch_bodies[0]
    assert "gateway_api_key" not in patch_bodies[0]


def test_claim_refuses_other_owner():
    gw, _http = _gateway(
        {
            ("GET", "https://example.supabase.co/rest/v1/slots"): FakeResponse(
                200,
                [{"id": "slot-1", "user_id": "other-user", "motherboard_slot_num": 1}],
            ),
        }
    )
    tenant = SupabaseTenantStore(gw)
    assert tenant.owner_of_slot(1) == "other-user"
    assert tenant.claim_slot(1, USER["id"], None) is False


def test_expired_invalid_and_missing_tokens():
    from infrastructure.supabase_jwt import sign_supabase_access_token

    secret = "unit-test-supabase-jwt-secret-not-for-production"
    gw, _http = _gateway({})
    svc = AuthService(supabase=gw, jwt_secret=secret)
    assert svc.validate_user_token(None) is None
    assert svc.validate_user_token("") is None
    assert svc.validate_user_token("not-a-jwt") is None
    expired = sign_supabase_access_token(
        user_id=USER["id"],
        email=USER["email"],
        secret=secret,
        ttl_seconds=1,
        now=1.0,
        issuer="https://example.supabase.co",
    )
    assert svc.validate_user_token(expired) is None


def test_unavailable_gotrue_returns_503():
    class DownHTTP:
        def request(self, *args, **kwargs):
            raise ConnectionError("down")

        def close(self) -> None:
            return None

    gw = SupabaseGateway(
        "https://example.supabase.co",
        "anon-test-key",
        session=DownHTTP(),  # type: ignore[arg-type]
    )
    svc = AuthService(supabase=gw)
    result = svc.login({"email": "owner@example.com", "password": STRONG})
    assert result.http_status == 503
    assert result.body["error"] == "auth_unavailable"
