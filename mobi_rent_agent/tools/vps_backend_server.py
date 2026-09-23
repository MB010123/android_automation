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

from application.webhook_outbound_dispatcher import WebhookOutboundDispatcher
from infrastructure.farm_sms_client import FarmSmsClient
from infrastructure.outbound_job_store import OutboundJobStore
from infrastructure.voidfix_webhook_http import (
    WebhookBodyError,
    parse_inbound_http_body,
    slot_for_voidfix_device,
)

HEALTH_PATH = "/health"
FARM_STATUS_PATH = "/farm/status"
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
    request_timeout: float = 10.0

    def log_message(self, fmt: str, *args) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, code: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
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
                row_id = self.inbound_store.insert(
                    device_id=msg.device_id,
                    slot_id=slot_id,
                    from_number=msg.from_number,
                    body=msg.message,
                    payload=payload,
                )
                stored_ids.append(row_id)
                logger.info("inbound_stored row_id=%s slot=%s", row_id, slot_id)
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

    Handler.app_name = config.app_name
    Handler.webhook_path = _env_webhook_path()
    Handler.webhook_secret = config.voidfix_webhook_secret
    Handler.device_map = device_map
    Handler.inbound_store = store
    Handler.outbound_dispatcher = dispatcher
    Handler.farm_agent_url = farm_url
    Handler.farm_agent_token = farm_token
    Handler.request_timeout = config.request_timeout_seconds

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    logger.info(
        "VPS backend http://%s:%s (health %s, farm proxy %s, webhook %s)",
        args.host,
        args.port,
        HEALTH_PATH,
        FARM_STATUS_PATH,
        Handler.webhook_path,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
    finally:
        store.close()
        job_store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
