"""VPS internet-facing backend: /health, VoidFix inbound webhook, farm status proxy.

Does not use ADB. SMS send remains on the physical PC agent.

  python tools/vps_backend_server.py
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.vps_api_contract import error_body
from infrastructure.inbound_message_store import InboundMessageStore
from infrastructure.voidfix_api import SmsGatewayError, parse_inbound_payload
from infrastructure.voidfix_devices import VoidFixDeviceMapError, load_voidfix_device_map

from application.inbound_sms_normalize import (
    normalize_inbound_for_lovable,
    provider_message_id_from_payload,
)
from application.farm_heartbeat_poller import FarmHeartbeatPoller
from application.vps_farm_management_service import VpsFarmManagementService
from application.vps_job_worker import VpsJobWorker
from application.auth_middleware import resolve_auth_context
from application.auth_service import AuthContext, AuthService
from application.vps_lovable_routes import (
    FARM_SERVICE_ONLY,
    PUBLIC_AUTH_POST,
    REMOTE_ACCESS_KINDS,
    USER_OWNED_KINDS,
    parse_route,
)
from application.remote_access_service import RemoteAccessService
from infrastructure.auth_rate_limiter import AuthRateLimiter
from infrastructure.supabase_gateway import gotrue_from_env
from infrastructure.lovable_tenant_store import tenant_store_from_env
from application.vps_slot_sms_service import VpsSlotSmsService
from application.webhook_outbound_dispatcher import WebhookOutboundDispatcher
from infrastructure.farm_sms_client import FarmSmsClient
from infrastructure.farm_task_client import FarmTaskClient
from infrastructure.lovable_inbound_webhook import deliver_inbound_to_lovable
from infrastructure.slot_assignment_store import SlotAssignmentStore
from infrastructure.slot_event_store import SlotEventStore
from infrastructure.slot_status_store import SlotStatusStore
from infrastructure.slot_public_id import public_id_for_farm_slot
from infrastructure.vps_job_store import VpsJobStore
from infrastructure.outbound_job_store import OutboundJobStore
from infrastructure.outbound_message_store import OutboundMessageStore
from infrastructure.slot_public_id import load_slot_public_id_overrides
from infrastructure.vps_rate_limiter import VpsRateLimiter
from infrastructure.vps_openapi_spec import build_vps_openapi_document
from infrastructure.vps_api_docs import (
    OPENAPI_JSON_PATH,
    REDOC_PATH,
    SWAGGER_UI_PATH,
    redoc_html,
    swagger_ui_html,
)
from infrastructure.voidfix_webhook_http import (
    WebhookBodyError,
    parse_inbound_http_body,
    slot_for_voidfix_device,
)

HEALTH_PATH = "/health"
FARM_STATUS_PATH = "/farm/status"
# Upper bound per background thread when shutting down (never indefinite).
SHUTDOWN_JOIN_TIMEOUT_SECONDS = 5.0
DEFAULT_WEBHOOK_PATH = "/voidfix/inbound"

logger = logging.getLogger("vps_backend")


def _env_webhook_path() -> str:
    raw = os.getenv("VOIDFIX_WEBHOOK_PATH", DEFAULT_WEBHOOK_PATH).strip()
    return raw if raw.startswith("/") else f"/{raw}"


def _parse_allowed_origins(raw: str) -> frozenset[str]:
    extras = {part.strip() for part in raw.split(",") if part.strip()}
    defaults = {
        "http://localhost",
        "https://localhost",
        "http://127.0.0.1",
        "https://127.0.0.1",
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:8080",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:8080",
    }
    return frozenset(defaults | extras)


def _auth_and_tenant_from_env() -> tuple[Any, Any]:
    gateway = gotrue_from_env()
    tenant = tenant_store_from_env(default_box=os.getenv("FARM_DEFAULT_BOX", "POD_01"))
    if gateway is None:
        logger.error("SUPABASE_URL and SUPABASE_ANON_KEY required; user token validation disabled")
    if not getattr(tenant, "privileged", False):
        logger.error(
            "LOVABLE_API_URL and VPS_TO_LOVABLE_API_TOKEN required; tenant reads/writes disabled"
        )
    return gateway, tenant


def _origin_is_localhost(origin: str) -> bool:
    parsed = urlparse(origin)
    if parsed.scheme not in {"http", "https"}:
        return False
    return parsed.hostname in {"localhost", "127.0.0.1"}


def _check_webhook_secret(provided: str | None, expected: str | None) -> None:
    if not expected:
        return
    if not authorize_farm_request(provided, expected):
        raise ValueError("inbound webhook secret mismatch")


def fetch_farm_status(farm_agent_url: str, api_token: str, timeout: float) -> dict[str, Any]:
    base = farm_agent_url.rstrip("/")
    url = f"{base}/agent/health"
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {api_token}"},
        timeout=timeout,
    )
    if response.status_code == 401:
        return {"ok": False, "error": "farm_auth_failed"}
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        return {"ok": False, "error": "invalid_farm_response"}
    return body


class Handler(BaseHTTPRequestHandler):
    app_name: str = "mobi-rent-agent"
    webhook_path: str = DEFAULT_WEBHOOK_PATH
    webhook_secret: str | None = None
    device_map: dict[int, str] = {}
    inbound_store: InboundMessageStore | None = None
    outbound_dispatcher: WebhookOutboundDispatcher | None = None
    farm_agent_url: str | None = None
    farm_agent_token: str | None = None
    farm_service_token: str | None = None
    slot_sms_service: VpsSlotSmsService | None = None
    farm_management_service: VpsFarmManagementService | None = None
    auth_service: AuthService | None = None
    auth_rate_limiter: AuthRateLimiter | None = None
    allowed_origins: frozenset[str] = frozenset()
    auth_cookie_name: str | None = None
    auth_cookie_secure: bool = False
    lovable_inbound_url: str | None = None
    lovable_inbound_secret: str | None = None
    request_timeout: float = 10.0
    tenant_store: Any = None
    remote_access_service: RemoteAccessService | None = None

    def log_message(self, fmt: str, *args) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _auth_context(self) -> AuthContext | None:
        return resolve_auth_context(
            authorization_header=self.headers.get("Authorization"),
            cookie_header=self.headers.get("Cookie"),
            cookie_name=self.auth_cookie_name,
            farm_service_token=self.farm_service_token,
            auth_service=self.auth_service,
        )

    def _cors_origin(self) -> str | None:
        origin = self.headers.get("Origin")
        if not origin:
            return None
        if origin in self.allowed_origins or _origin_is_localhost(origin):
            return origin
        return None

    def _apply_cors(self) -> None:
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _read_json_body(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 65536:
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            return None
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def _send_json(self, code: int, body: dict[str, Any], *, set_auth_cookie: str | None = None) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self._apply_cors()
        if set_auth_cookie is not None and self.auth_cookie_name:
            flags = "HttpOnly; Path=/; SameSite=Lax"
            if self.auth_cookie_secure:
                flags += "; Secure"
            if set_auth_cookie == "":
                self.send_header(
                    "Set-Cookie",
                    f"{self.auth_cookie_name}=; {flags}; Max-Age=0",
                )
            else:
                ttl = self.auth_service.access_ttl_seconds if self.auth_service else 3600
                self.send_header(
                    "Set-Cookie",
                    f"{self.auth_cookie_name}={set_auth_cookie}; {flags}; Max-Age={ttl}",
                )
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, code: int, html: str) -> None:
        raw = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._apply_cors()
        self.end_headers()
        self.wfile.write(raw)

    def _user_owns(self, ctx: AuthContext, slot_public_id: str | None) -> bool:
        if ctx.kind != "user" or ctx.user is None or not slot_public_id:
            return False
        if self.farm_management_service is None:
            return False
        return self.farm_management_service.user_owns_public_slot(ctx.user.user_id, slot_public_id)

    def _rate_limit_auth(self, action: str, email: str | None) -> bool:
        if self.auth_rate_limiter is None:
            return True
        ip = self.client_address[0] if self.client_address else "unknown"
        allowed, retry = self.auth_rate_limiter.check(f"{action}:ip:{ip}")
        if not allowed:
            body = error_body("rate_limited")
            body["retry_after"] = retry
            self._send_json(429, body)
            return False
        if email:
            allowed, retry = self.auth_rate_limiter.check(f"{action}:email:{email.strip().lower()}")
            if not allowed:
                body = error_body("rate_limited")
                body["retry_after"] = retry
                self._send_json(429, body)
                return False
        return True

    def _authorize_route(self, route) -> AuthContext | None:
        ctx = self._auth_context()
        kind = route.kind
        if kind in PUBLIC_AUTH_POST:
            return ctx or AuthContext(kind="none")
        if kind in {"auth_logout", "auth_me", "auth_resend"}:
            if ctx is None or ctx.kind != "user":
                self._send_json(401, {"error": "unauthorized"})
                return None
            return ctx
        if kind == "auth_session":
            if ctx is None or ctx.kind not in {"user", "farm_service"}:
                self._send_json(401, {"error": "unauthorized"})
                return None
            return ctx
        if kind in FARM_SERVICE_ONLY:
            if ctx is None or ctx.kind != "farm_service":
                self._send_json(401, {"error": "unauthorized"})
                return None
            return ctx
        if kind in REMOTE_ACCESS_KINDS:
            # Customer routes need an authenticated user; only `release`
            # (end of rental) may also come from the Lovable farm-service credential.
            allowed_kinds = {"user"}
            if kind == "remote_access_action" and route.action == "release":
                allowed_kinds.add("farm_service")
            if ctx is None or ctx.kind not in allowed_kinds:
                self._send_json(401, {"error": "unauthorized"})
                return None
            return ctx
        if ctx is None or ctx.kind not in {"user", "farm_service"}:
            self._send_json(401, {"error": "unauthorized"})
            return None
        if (
            ctx.kind == "user"
            and kind in USER_OWNED_KINDS
            and route.slot_public_id
            and not self._user_owns(ctx, route.slot_public_id)
        ):
            self._send_json(404, error_body("slot_not_found"))
            return None
        return ctx

    def _handle_api_docs(self, path: str) -> bool:
        if path == OPENAPI_JSON_PATH:
            spec = build_vps_openapi_document(voidfix_webhook_path=self.webhook_path)
            self._send_json(200, spec)
            return True
        if path == SWAGGER_UI_PATH:
            self._send_html(200, swagger_ui_html())
            return True
        if path == REDOC_PATH:
            self._send_html(200, redoc_html())
            return True
        return False

    def _handle_remote_access(self, route, ctx: AuthContext, payload: dict[str, Any] | None) -> bool:
        """Remote-access POC. All authorization happens inside the service.

        The browser only supplies the rental id in the path; slot and device
        are derived server-side. ``payload`` is ignored on purpose so a
        client can never smuggle a device_id/slot_id.
        """
        del payload
        service = self.remote_access_service
        if service is None or not service.enabled:
            self._send_json(403, error_body("forbidden"))
            return True
        rental_id = route.rental_id or ""
        method = self.command
        customer_id = ctx.user.user_id if (ctx.kind == "user" and ctx.user is not None) else None
        if route.kind == "remote_access":
            if method == "GET":
                result = service.get_remote_access(customer_id or "", None, rental_id)
            elif method == "POST":
                result = service.create_remote_access(customer_id or "", None, rental_id)
            else:
                self._send_json(405, {"ok": False, "error": "method_not_allowed"})
                return True
            self._send_json(result.http_status, result.body)
            return True
        if method != "POST":
            self._send_json(405, {"ok": False, "error": "method_not_allowed"})
            return True
        action = route.action
        if action == "revoke":
            result = service.revoke_remote_access(customer_id or "", None, rental_id)
        elif action == "release":
            if ctx.kind == "farm_service":
                slot_id = self._release_slot_for_rental(service, rental_id)
                if slot_id is None:
                    self._send_json(403, error_body("forbidden"))
                    return True
                result = service.release_device(slot_id, rental_id)
            else:
                # A customer ending their own rental: revoke + release own slot.
                result = service.revoke_remote_access(customer_id or "", None, rental_id)
                if result.http_status == 200:
                    slot = result.body.get("slot_id")
                    if isinstance(slot, int):
                        result = service.release_device(slot, rental_id)
        elif action == "device-status":
            result = service.device_status_for_customer(customer_id or "", rental_id)
        elif action == "reboot":
            result = service.reboot_for_customer(customer_id or "", rental_id)
        elif action == "prepare-esim":
            result = service.prepare_esim(customer_id or "", rental_id)
        elif action == "activation-status":
            result = service.activation_status_for_customer(customer_id or "", rental_id)
        else:
            self._send_json(404, {"error": "not_found"})
            return True
        self._send_json(result.http_status, result.body)
        return True

    def _release_slot_for_rental(self, service: RemoteAccessService, rental_id: str) -> int | None:
        """Farm-service release: slot comes from the tenant rental row, never the request."""
        getter = getattr(self.tenant_store, "get_slot_by_id", None)
        if not callable(getter):
            return None
        try:
            row = getter(rental_id)
        except (TypeError, ValueError, OSError, requests.RequestException):
            return None
        if not isinstance(row, dict):
            return None
        for key in ("motherboard_slot_num", "bay"):
            try:
                if row.get(key) is not None:
                    slot = int(row.get(key))
                    return slot if slot in service.allowed_slot_ids else None
            except (TypeError, ValueError):
                continue
        return None

    def _handle_service_get(self, route) -> bool:
        query = parse_qs(urlparse(self.path).query)
        if route.kind == "farm_available":
            assert self.farm_management_service is not None
            result = self.farm_management_service.list_available_slots()
            self._send_json(result.http_status, result.body)
            return True
        if route.kind == "job" and route.job_id:
            assert self.farm_management_service is not None
            result = self.farm_management_service.get_job(route.job_id)
            ctx = self._auth_context()
            if (
                ctx is not None
                and ctx.kind == "user"
                and result.http_status == 200
                and not self._user_owns(ctx, result.body.get("slot_id"))
            ):
                self._send_json(404, error_body("job_not_found"))
                return True
            self._send_json(result.http_status, result.body)
            return True
        if route.kind == "message" and route.message_id:
            assert self.slot_sms_service is not None
            ctx = self._auth_context()
            if ctx is not None and ctx.kind == "user" and ctx.user is not None:
                result = self.slot_sms_service.get_tenant_message(
                    route.message_id,
                    user_id=ctx.user.user_id,
                )
            else:
                result = self.slot_sms_service.get_message(route.message_id)
            self._send_json(result.http_status, result.body)
            return True
        if route.kind == "slot_messages" and route.slot_public_id:
            assert self.slot_sms_service is not None
            limit_raw = (query.get("limit") or ["50"])[0]
            try:
                limit = int(limit_raw)
            except ValueError:
                self._send_json(400, {"error": "invalid_limit"})
                return True
            cursor = (query.get("cursor") or [None])[0]
            direction = (query.get("direction") or [None])[0]
            result = self.slot_sms_service.list_messages(
                route.slot_public_id,
                limit=limit,
                cursor=cursor,
                direction=direction,
            )
            self._send_json(result.http_status, result.body)
            return True
        if route.kind == "slot_events" and route.slot_public_id:
            assert self.farm_management_service is not None
            result = self.farm_management_service.list_events(
                route.slot_public_id,
                since_raw=(query.get("since") or [None])[0],
                limit_raw=(query.get("limit") or [None])[0],
            )
            self._send_json(result.http_status, result.body)
            return True
        if route.kind == "slot_status" and route.slot_public_id:
            assert self.farm_management_service is not None
            result = self.farm_management_service.get_slot_status(route.slot_public_id)
            self._send_json(result.http_status, result.body)
            return True
        if route.kind == "slots_list":
            assert self.farm_management_service is not None
            ctx = self._auth_context()
            if ctx is not None and ctx.kind == "user" and ctx.user is not None:
                result = self.farm_management_service.list_slots_for_user(ctx.user.user_id)
            else:
                result = self.farm_management_service.list_all_slots()
            self._send_json(result.http_status, result.body)
            return True
        if route.kind == "slot_detail" and route.slot_public_id:
            assert self.farm_management_service is not None
            result = self.farm_management_service.get_slot_record(route.slot_public_id)
            self._send_json(result.http_status, result.body)
            return True
        if route.kind == "auth_session":
            ctx = self._auth_context() or AuthContext(kind="none")
            if ctx.kind == "farm_service":
                if self.auth_service is not None:
                    result = self.auth_service.session_body(ctx)
                    self._send_json(result.http_status, result.body)
                    return True
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "authenticated": True,
                        "audience": "farm_service",
                        "token_type": "FARM_SERVICE_TOKEN",
                        "message": "Machine-to-machine farm service credential. Not a user session.",
                    },
                )
                return True
            if self.auth_service is None:
                self._send_json(503, {"ok": False, "error": "auth_not_configured"})
                return True
            result = self.auth_service.session_body(ctx)
            self._send_json(result.http_status, result.body)
            return True
        if route.kind == "auth_me":
            if self.auth_service is None:
                self._send_json(503, {"ok": False, "error": "auth_not_configured"})
                return True
            result = self.auth_service.me(self._auth_context() or AuthContext(kind="none"))
            self._send_json(result.http_status, result.body)
            return True
        return False

    def _handle_service_post(self, route, payload: dict[str, Any]) -> bool:
        if route.kind == "farm_assign" and route.farm_bay is not None:
            assert self.farm_management_service is not None
            result = self.farm_management_service.assign_slot(route.farm_bay, payload)
            self._send_json(result.http_status, result.body)
            return True
        if route.kind == "slot_action" and route.slot_public_id and route.action:
            assert self.farm_management_service is not None
            result = self.farm_management_service.enqueue_action(
                route.slot_public_id,
                route.action,
                payload,
            )
            self._send_json(result.http_status, result.body)
            return True
        if route.kind == "slot_sms_send" and route.slot_public_id:
            assert self.slot_sms_service is not None
            result = self.slot_sms_service.enqueue_send(route.slot_public_id, payload)
            self._send_json(result.http_status, result.body)
            return True
        if route.kind == "slot_esim" and route.slot_public_id:
            assert self.farm_management_service is not None
            ctx = self._auth_context()
            if ctx is not None and ctx.kind == "user" and ctx.user is not None:
                result = self.farm_management_service.assign_esim_for_user(
                    route.slot_public_id,
                    payload,
                    ctx.user.user_id,
                )
            else:
                result = self.farm_management_service.assign_slot_by_public_id(
                    route.slot_public_id,
                    payload,
                )
            self._send_json(result.http_status, result.body)
            return True
        return False

    def _handle_auth_post(self, route, payload: dict[str, Any]) -> bool:
        auth_kinds = PUBLIC_AUTH_POST | {"auth_logout", "auth_resend"}
        if route.kind not in auth_kinds:
            return False
        if self.auth_service is None:
            self._send_json(503, {"ok": False, "error": "auth_not_configured"})
            return True
        ip = self.client_address[0] if self.client_address else None
        user_agent = self.headers.get("User-Agent")
        email = payload.get("email") if isinstance(payload.get("email"), str) else None
        if route.kind == "auth_signup":
            if not self._rate_limit_auth("signup", email):
                return True
            result = self.auth_service.signup(payload, ip=ip, user_agent=user_agent)
            self._send_json(result.http_status, result.body, set_auth_cookie=result.access_token)
            return True
        if route.kind == "auth_login":
            if not self._rate_limit_auth("login", email):
                return True
            result = self.auth_service.login(payload, ip=ip, user_agent=user_agent)
            self._send_json(result.http_status, result.body, set_auth_cookie=result.access_token)
            return True
        if route.kind == "auth_logout":
            if not self._rate_limit_auth("logout", None):
                return True
            ctx = self._auth_context() or AuthContext(kind="none")
            result = self.auth_service.logout(ctx)
            self._send_json(result.http_status, result.body, set_auth_cookie="")
            return True
        if route.kind == "auth_forgot":
            if not self._rate_limit_auth("forgot", email):
                return True
            result = self.auth_service.forgot_password(payload)
            self._send_json(result.http_status, result.body)
            return True
        if route.kind == "auth_reset":
            if not self._rate_limit_auth("reset", None):
                return True
            result = self.auth_service.reset_password(payload)
            self._send_json(result.http_status, result.body)
            return True
        if route.kind == "auth_verify":
            result = self.auth_service.verify_email(payload)
            self._send_json(result.http_status, result.body)
            return True
        if route.kind == "auth_resend":
            ctx = self._auth_context() or AuthContext(kind="none")
            result = self.auth_service.resend_verification(ctx)
            self._send_json(result.http_status, result.body)
            return True
        return False

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._apply_cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if self._handle_api_docs(path):
            return
        route = parse_route(path)
        if route is not None:
            if route.kind in PUBLIC_AUTH_POST or route.kind in {"auth_logout", "auth_resend"}:
                self._send_json(405, {"ok": False, "error": "method_not_allowed"})
                return
            ctx = self._authorize_route(route)
            if ctx is None:
                return
            if route.kind in REMOTE_ACCESS_KINDS:
                self._handle_remote_access(route, ctx, None)
                return
            if self._handle_service_get(route):
                return
            self._send_json(404, {"error": "not_found"})
            return
        if path == HEALTH_PATH:
            self._send_json(
                200,
                {
                    "ok": True,
                    "role": "vps",
                    "service": self.app_name,
                    "webhook_path": self.webhook_path,
                    "inbound_stored": self.inbound_store.count() if self.inbound_store else 0,
                },
            )
            return
        if path == FARM_STATUS_PATH:
            if not self.farm_agent_url or not self.farm_agent_token:
                self._send_json(
                    503,
                    {"ok": False, "error": "farm_agent_not_configured"},
                )
                return
            try:
                farm = fetch_farm_status(
                    self.farm_agent_url,
                    self.farm_agent_token,
                    self.request_timeout,
                )
            except requests.RequestException as exc:
                self._send_json(502, {"ok": False, "error": "farm_unreachable", "detail": str(exc)})
                return
            self._send_json(200, {"ok": True, "role": "vps", "farm": farm})
            return
        if path == self.webhook_path:
            self._send_json(
                200,
                {"ok": True, "method": "GET", "hint": "POST VoidFix webhook to this path"},
            )
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        route = parse_route(path)
        if route is not None:
            if route.kind in {"auth_me", "auth_session"}:
                self._send_json(405, {"ok": False, "error": "method_not_allowed"})
                return
            ctx: AuthContext | None = None
            if route.kind not in PUBLIC_AUTH_POST:
                ctx = self._authorize_route(route)
                if ctx is None:
                    return
            payload = self._read_json_body()
            if payload is None:
                self._send_json(400, {"error": "invalid_json"})
                return
            if route.kind in REMOTE_ACCESS_KINDS and ctx is not None:
                self._handle_remote_access(route, ctx, payload)
                return
            if self._handle_auth_post(route, payload):
                return
            if self._handle_service_post(route, payload):
                return
            self._send_json(404, {"error": "not_found"})
            return
        if path != self.webhook_path:
            self._send_json(404, {"ok": False, "error": "not found"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        query = parse_qs(urlparse(self.path).query)
        provided_secret = (
            (query.get("secret") or [None])[0]
            or self.headers.get("X-Webhook-Secret")
            or self.headers.get("X-Voidfix-Webhook-Secret")
        )

        try:
            _check_webhook_secret(provided_secret, self.webhook_secret)
            payload = parse_inbound_http_body(raw, self.headers.get("Content-Type"))
            messages = parse_inbound_payload(payload)
        except WebhookBodyError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        except ValueError as exc:
            self._send_json(401, {"ok": False, "error": str(exc)})
            return
        except SmsGatewayError as exc:
            self._send_json(422, {"ok": False, "error": str(exc)})
            return

        logger.info("webhook_received parsed_count=%s", len(messages))
        stored_ids: list[int] = []
        mapped_slots: list[int | None] = []
        dispatch_results: list[dict[str, Any]] = []
        for index, msg in enumerate(messages):
            slot_id = slot_for_voidfix_device(msg.device_id, self.device_map)
            mapped_slots.append(slot_id)
            row_id: int | None = None
            if self.inbound_store is not None:
                provider_id = provider_message_id_from_payload(payload, index)
                slot_public = (
                    public_id_for_farm_slot(int(slot_id)) if slot_id is not None else None
                )
                row_id, created = self.inbound_store.insert(
                    device_id=msg.device_id,
                    slot_id=slot_id,
                    from_number=msg.from_number,
                    body=msg.message,
                    payload=payload,
                    provider_message_id=provider_id,
                    slot_public_id=slot_public,
                )
                stored_ids.append(row_id)
                logger.info("inbound_stored row_id=%s slot=%s created=%s", row_id, slot_id, created)
                if created and slot_id is not None and self.tenant_store is not None:
                    inbound_id = provider_id or f"inbound-{row_id}"
                    self.tenant_store.record_message(
                        farm_slot_id=int(slot_id),
                        message_id=str(inbound_id),
                        direction="inbound",
                        phone_number=msg.from_number,
                        message_body=msg.message,
                        status="received",
                    )
                if created and slot_id is not None:
                    normalized = normalize_inbound_for_lovable(
                        msg,
                        farm_slot=slot_id,
                        provider_message_id=provider_id,
                    )
                    if self.lovable_inbound_url and self.lovable_inbound_secret:
                        threading.Thread(
                            target=deliver_inbound_to_lovable,
                            args=(
                                self.lovable_inbound_url,
                                self.lovable_inbound_secret,
                                normalized,
                            ),
                            kwargs={"timeout": self.request_timeout},
                            daemon=True,
                        ).start()
                    if self.farm_management_service is not None:
                        self.farm_management_service.record_event(
                            int(slot_id),
                            "inbound_sms_received",
                            "provider_message_id=" + (provider_id or "unknown"),
                        )
            if self.outbound_dispatcher is not None:
                dispatch_results.append(
                    self.outbound_dispatcher.process_inbound(
                        msg,
                        inbound_row_id=row_id,
                        inbound_slot=slot_id,
                        payload=payload,
                        index=index,
                    )
                )

        self._send_json(
            200,
            {
                "ok": True,
                "parsed_count": len(messages),
                "device_ids": [m.device_id for m in messages],
                "mapped_slots": mapped_slots,
                "stored_ids": stored_ids,
                "dispatch": dispatch_results,
            },
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host",
        default=os.getenv("VPS_BACKEND_LISTEN_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("VPS_BACKEND_LISTEN_PORT", "8080")),
    )
    parser.add_argument(
        "--env-file",
        default=os.getenv("MOBI_RENT_ENV_FILE", str(ROOT / ".env")),
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from infrastructure.config import ConfigError, load_config

    env_path = Path(args.env_file)
    try:
        config = load_config(env_file=str(env_path) if env_path.exists() else None)
    except ConfigError as exc:
        logger.error("config: %s", exc)
        return 1

    device_map: dict[int, str] = {}
    map_path = config.voidfix_device_map_path
    if map_path:
        try:
            device_map = load_voidfix_device_map(
                Path(map_path) if not Path(map_path).is_absolute() else map_path
            )
        except VoidFixDeviceMapError as exc:
            logger.error("voidfix device map: %s", exc)
            return 1
    else:
        logger.warning("VOIDFIX_DEVICE_MAP_PATH unset; inbound slot mapping will be null")

    inbound_db = os.getenv(
        "INBOUND_MESSAGES_DB_PATH",
        str(ROOT / "logs" / "inbound_messages.sqlite"),
    )
    store = InboundMessageStore(Path(inbound_db))

    outbound_db = os.getenv(
        "OUTBOUND_JOBS_DB_PATH",
        str(ROOT / "logs" / "outbound_jobs.sqlite"),
    )
    job_store = OutboundJobStore(Path(outbound_db))
    farm_url = os.getenv("FARM_AGENT_URL") or None
    farm_token = os.getenv("FARM_AGENT_API_TOKEN") or None
    farm_client: FarmSmsClient | None = None
    if farm_url and farm_token:
        farm_client = FarmSmsClient(
            farm_url,
            farm_token,
            timeout_seconds=config.webhook_farm_dispatch_timeout_seconds,
        )
    dispatcher = WebhookOutboundDispatcher(
        job_store=job_store,
        farm_client=farm_client,
        max_dispatch_attempts=config.webhook_farm_dispatch_max_attempts,
    )

    api_messages_db = os.getenv(
        "OUTBOUND_MESSAGES_DB_PATH",
        str(ROOT / "logs" / "outbound_api_messages.sqlite"),
    )
    message_store = OutboundMessageStore(Path(api_messages_db))
    known_slots = set(device_map.keys()) if device_map else set(range(1, 21))
    overrides = load_slot_public_id_overrides(os.getenv("SLOT_PUBLIC_ID_MAP_PATH"))

    def _farm_status() -> dict[str, Any]:
        if not farm_url or not farm_token:
            return {"ok": False, "error": "farm_agent_not_configured"}
        return fetch_farm_status(farm_url, farm_token, config.request_timeout_seconds)

    vps_jobs_db = os.getenv(
        "VPS_JOBS_DB_PATH",
        str(ROOT / "logs" / "vps_jobs.sqlite"),
    )
    vps_job_store = VpsJobStore(Path(vps_jobs_db))
    assignment_store = SlotAssignmentStore(Path(vps_jobs_db).with_name("slot_assignments.sqlite"))
    event_store = SlotEventStore(Path(vps_jobs_db).with_name("slot_events.sqlite"))
    farm_task_client: FarmTaskClient | None = None
    if farm_url and farm_token:
        farm_task_client = FarmTaskClient(
            farm_url,
            farm_token,
            timeout_seconds=config.webhook_farm_dispatch_timeout_seconds,
        )
    gateway, tenant_store = _auth_and_tenant_from_env()
    job_worker = VpsJobWorker(
        job_store=vps_job_store,
        assignment_store=assignment_store,
        event_store=event_store,
        farm_task_client=farm_task_client,
        auth_store=tenant_store,
        running_stale_seconds=config.webhook_farm_dispatch_timeout_seconds,
    )
    job_worker.start()
    mgmt_rate = VpsRateLimiter(
        per_slot_limit=int(os.getenv("VPS_MGMT_RATE_LIMIT_PER_SLOT", "20")),
        global_limit=int(os.getenv("VPS_MGMT_RATE_LIMIT_GLOBAL", "200")),
    )
    heartbeat_interval = float(os.getenv("VPS_FARM_HEARTBEAT_INTERVAL_SECONDS", "30"))
    status_store = SlotStatusStore(Path(vps_jobs_db).with_name("slot_status.sqlite"))
    heartbeat_poller = FarmHeartbeatPoller(
        farm_status_fetcher=_farm_status,
        status_store=status_store,
        event_store=event_store,
        known_farm_slots=known_slots,
        interval_seconds=heartbeat_interval,
    )
    heartbeat_poller.start()
    auth_prefixes = tuple(
        p.strip()
        for p in (os.getenv("VPS_ESIM_ALLOWED_URL_PREFIXES") or "").split(",")
        if p.strip()
    )
    auth_service: AuthService | None = None
    if gateway is not None:
        storage_prefix = f"{gateway.project_url}/storage/v1/object/"
        if storage_prefix not in auth_prefixes:
            auth_prefixes = auth_prefixes + (storage_prefix,)
        auth_service = AuthService(
            supabase=gateway,
            tenant=tenant_store,
            jwt_secret=(os.getenv("SUPABASE_JWT_SECRET") or "").strip(),
            access_ttl_seconds=int(os.getenv("VPS_AUTH_ACCESS_TOKEN_TTL_SECONDS", "3600")),
        )
    farm_management_service = VpsFarmManagementService(
        job_store=vps_job_store,
        assignment_store=assignment_store,
        event_store=event_store,
        job_worker=job_worker,
        farm_status_fetcher=_farm_status,
        known_farm_slots=known_slots,
        slot_id_overrides=overrides,
        default_box=os.getenv("FARM_DEFAULT_BOX", "POD_01"),
        rate_limiter=mgmt_rate,
        status_store=status_store,
        heartbeat_interval_seconds=heartbeat_interval,
        auth_store=tenant_store,
        esim_url_prefixes=auth_prefixes,
    )

    sms_rate_limiter = VpsRateLimiter(
        per_slot_limit=int(os.getenv("VPS_SMS_RATE_LIMIT_PER_SLOT", "10")),
        global_limit=int(os.getenv("VPS_SMS_RATE_LIMIT_GLOBAL", "120")),
    )
    slot_sms_service = VpsSlotSmsService(
        message_store=message_store,
        farm_client=farm_client,
        farm_status_fetcher=_farm_status,
        known_farm_slots=known_slots,
        slot_id_overrides=overrides,
        rate_limiter=sms_rate_limiter,
        max_dispatch_attempts=config.webhook_farm_dispatch_max_attempts,
        event_recorder=event_store.append,
        tenant_store=tenant_store,
    )

    remote_access_service, remote_access_store = _build_remote_access_service(
        config,
        tenant_store=tenant_store,
        farm_task_client=farm_task_client,
        farm_status_fetcher=_farm_status,
        event_store=event_store,
        esim_url_prefixes=auth_prefixes,
        db_path=Path(vps_jobs_db).with_name("remote_access_sessions.sqlite"),
    )
    if remote_access_service is not None:
        heartbeat_poller.set_after_poll(remote_access_service.sweep_stale_sessions)

    Handler.app_name = config.app_name
    Handler.webhook_path = _env_webhook_path()
    Handler.webhook_secret = config.voidfix_webhook_secret
    Handler.device_map = device_map
    Handler.inbound_store = store
    Handler.outbound_dispatcher = dispatcher
    Handler.farm_agent_url = farm_url
    Handler.farm_agent_token = farm_token
    Handler.farm_service_token = os.getenv("FARM_SERVICE_TOKEN") or None
    Handler.slot_sms_service = slot_sms_service
    Handler.farm_management_service = farm_management_service
    Handler.auth_service = auth_service
    Handler.tenant_store = tenant_store
    Handler.remote_access_service = remote_access_service
    Handler.auth_rate_limiter = AuthRateLimiter(
        limit=int(os.getenv("VPS_AUTH_RATE_LIMIT", "10")),
        window_seconds=float(os.getenv("VPS_AUTH_RATE_WINDOW_SECONDS", "60")),
        max_keys=int(os.getenv("VPS_AUTH_RATE_MAX_KEYS", "4096")),
    )
    Handler.allowed_origins = _parse_allowed_origins(os.getenv("VPS_ALLOWED_ORIGINS", ""))
    Handler.auth_cookie_name = os.getenv("VPS_AUTH_COOKIE_NAME") or None
    Handler.auth_cookie_secure = os.getenv("VPS_AUTH_COOKIE_SECURE", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    Handler.lovable_inbound_url = os.getenv("LOVABLE_INBOUND_WEBHOOK_URL") or None
    Handler.lovable_inbound_secret = os.getenv("LOVABLE_INBOUND_WEBHOOK_HMAC_SECRET") or None
    Handler.request_timeout = config.request_timeout_seconds

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    logger.info(
        "VPS backend http://%s:%s (health %s, farm proxy %s, webhook %s, docs %s, openapi %s)",
        args.host,
        args.port,
        HEALTH_PATH,
        FARM_STATUS_PATH,
        Handler.webhook_path,
        SWAGGER_UI_PATH,
        OPENAPI_JSON_PATH,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
    finally:
        # Stop background workers (bounded join) before closing the stores they write to.
        heartbeat_poller.stop(join_timeout=SHUTDOWN_JOIN_TIMEOUT_SECONDS)
        job_worker.stop(join_timeout=SHUTDOWN_JOIN_TIMEOUT_SECONDS)
        store.close()
        job_store.close()
        message_store.close()
        vps_job_store.close()
        assignment_store.close()
        event_store.close()
        status_store.close()
        if remote_access_store is not None:
            remote_access_store.close()
        if tenant_store is not None:
            tenant_store.close()
    return 0


def _build_remote_access_service(
    config: Any,
    *,
    tenant_store: Any,
    farm_task_client: FarmTaskClient | None,
    farm_status_fetcher: Any,
    event_store: SlotEventStore,
    esim_url_prefixes: tuple[str, ...],
    db_path: Path,
) -> tuple[RemoteAccessService | None, Any]:
    """Slot-1 remote-access POC. Returns (None, None) unless REMOTE_ACCESS_POC_ENABLED=true.

    The slot -> device serial map is the existing gitignored ``slot_map.json``
    (``SLOT_MAP_PATH``); only allowlisted slots are ever loaded into the
    remote-access layer. Missing map or platform config fails closed (403/503).
    """
    if not getattr(config, "remote_access_poc_enabled", False):
        return None, None
    from infrastructure.adb_slot_status import SlotMapError, load_slot_map
    from infrastructure.gads_remote_access import gads_platform_from_config
    from infrastructure.remote_access_store import RemoteAccessSessionStore

    allowed = tuple(getattr(config, "remote_access_poc_slot_ids", ()) or ())
    slot_map: dict[int, str] = {}
    map_path = Path(config.slot_map_path) if config.slot_map_path else ROOT / "slot_map.json"
    try:
        full_map = load_slot_map(map_path)
    except SlotMapError as exc:
        logger.error("remote_access: slot map unavailable (%s); POC routes will return 403", exc)
        full_map = {}
    slot_map = {slot: serial for slot, serial in full_map.items() if slot in allowed}
    platform = gads_platform_from_config(config)
    if platform is None:
        logger.error(
            "remote_access: REMOTE_ACCESS_PLATFORM_URL / ADMIN_USERNAME / ADMIN_PASSWORD / WORKSPACE_ID "
            "incomplete; POC routes will return 503"
        )
    store = RemoteAccessSessionStore(db_path)
    interrupted = store.reconcile_interrupted_prepares()
    if interrupted:
        logger.info("remote_access_prepare_reconciled count=%s", interrupted)
    service = RemoteAccessService(
        enabled=True,
        allowed_slot_ids=allowed,
        slot_device_map=slot_map,
        platform=platform,
        store=store,
        tenant_store=tenant_store,
        farm_task_client=farm_task_client,
        farm_status_fetcher=farm_status_fetcher,
        event_recorder=event_store.append,
        esim_url_prefixes=esim_url_prefixes,
        session_ttl_minutes=int(getattr(config, "remote_access_session_ttl_minutes", 60)),
        reboot_timeout_seconds=float(getattr(config, "remote_access_reboot_timeout_seconds", 180.0)),
    )
    logger.warning(
        "remote_access POC ENABLED for slots %s (mapped: %s); all other slots are refused",
        sorted(allowed),
        sorted(slot_map),
    )
    return service, store


if __name__ == "__main__":
    raise SystemExit(main())
