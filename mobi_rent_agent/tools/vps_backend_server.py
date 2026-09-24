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

from infrastructure.farm_agent_auth import authorize_farm_request, extract_bearer_token
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
from application.vps_lovable_routes import parse_route
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
    lovable_inbound_url: str | None = None
    lovable_inbound_secret: str | None = None
    request_timeout: float = 10.0

    def log_message(self, fmt: str, *args) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _authorized_service(self) -> bool:
        token = extract_bearer_token(self.headers.get("Authorization"))
        return authorize_farm_request(token, self.farm_service_token)

    def _read_json_body(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def _send_json(self, code: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, code: int, html: str) -> None:
        raw = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

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
            self._send_json(result.http_status, result.body)
            return True
        if route.kind == "message" and route.message_id:
            assert self.slot_sms_service is not None
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
        return False

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if self._handle_api_docs(path):
            return
        route = parse_route(path)
        if route is not None:
            if not self._authorized_service():
                self._send_json(401, {"error": "unauthorized"})
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
            if not self._authorized_service():
                self._send_json(401, {"error": "unauthorized"})
                return
            payload = self._read_json_body()
            if payload is None:
                self._send_json(400, {"error": "invalid_json"})
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
    job_worker = VpsJobWorker(
        job_store=vps_job_store,
        assignment_store=assignment_store,
        event_store=event_store,
        farm_task_client=farm_task_client,
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
    )

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
