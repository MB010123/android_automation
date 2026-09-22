"""HTTP listener for VoidFix inbound SMS webhooks.

Runs separately from ``main.py``. POST JSON or ``application/x-www-form-urlencoded``
(with ``messages=``) to the webhook path; forwards parsed payloads to
``SmsDispatchService.ingest_inbound()`` when VoidFix SMS is configured.

Production: bind loopback and terminate TLS on nginx/Caddy (see DEPLOYMENT.md).

  python tools/voidfix_inbound_webhook_listener.py --port 8787
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


from infrastructure.voidfix_webhook_http import (  # noqa: E402
    WebhookBodyError,
    parse_inbound_http_body,
    slot_for_voidfix_device,
)

DEFAULT_PORT = 8787
DEFAULT_WEBHOOK_PATH = "/voidfix/inbound"
WEBHOOK_PATH = DEFAULT_WEBHOOK_PATH  # backwards compatibility for tests/tools
HEALTH_PATH = "/health"

logger = logging.getLogger("voidfix_inbound_webhook")


def _env_webhook_path() -> str:
    raw = os.getenv("VOIDFIX_WEBHOOK_PATH", DEFAULT_WEBHOOK_PATH).strip()
    return raw if raw.startswith("/") else f"/{raw}"


def _env_capture_dir(root: Path) -> Path | None:
    raw = os.getenv("VOIDFIX_WEBHOOK_CAPTURE_DIR")
    if raw is not None and not str(raw).strip():
        return None
    if raw is None:
        return root / "logs" / "voidfix_inbound_webhook_captures"
    path = Path(raw)
    return path if path.is_absolute() else root / path


def _load_dispatch_service_with_config(config):
    from application.sms_factory import build_sms_dispatch_service

    return build_sms_dispatch_service(config)


class Handler(BaseHTTPRequestHandler):
    dispatch_service = None
    capture_dir: Path | None = None
    webhook_path: str = DEFAULT_WEBHOOK_PATH
    app_name: str = "mobi-rent-agent"

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
                    "service": self.app_name,
                    "webhook_path": self.webhook_path,
                },
            )
            return
        if path == self.webhook_path:
            self._send_json(
                200,
                {
                    "ok": True,
                    "method": "GET",
                    "hint": "POST JSON or form-urlencoded (messages=) to this path",
                },
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

        content_type = self.headers.get("Content-Type")
        try:
            payload = parse_inbound_http_body(raw, content_type)
        except WebhookBodyError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return

        if self.capture_dir is not None:
            stamp = __import__("time").time()
            out = self.capture_dir / f"inbound_{int(stamp * 1000)}.json"
            out.write_text(
                json.dumps(
                    {
                        "headers": {
                            k: v
                            for k, v in self.headers.items()
                            if k.lower() not in {"authorization", "cookie"}
                        },
                        "query": dict(query),
                        "content_type": content_type,
                        "payload": payload,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

        service = self.dispatch_service
        if service is None:
            self._send_json(
                503,
                {
                    "ok": False,
                    "error": "SmsDispatchService not configured (check .env VOIDFIX_*)",
                    "captured": self.capture_dir is not None,
                },
            )
            return

        try:
            messages = service.ingest_inbound(payload, provided_secret=provided_secret)
        except ValueError as exc:
            self._send_json(401, {"ok": False, "error": str(exc)})
            return
        except Exception as exc:
            self._send_json(422, {"ok": False, "error": str(exc)})
            return

        self._send_json(
            200,
            {
                "ok": True,
                "parsed_count": len(messages),
                "device_ids": [m.device_id for m in messages],
            },
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host",
        default=os.getenv("VOIDFIX_WEBHOOK_LISTEN_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("VOIDFIX_WEBHOOK_LISTEN_PORT", str(DEFAULT_PORT))),
    )
    parser.add_argument(
        "--webhook-path",
        default=_env_webhook_path(),
        help="URL path for POST webhooks (env VOIDFIX_WEBHOOK_PATH)",
    )
    parser.add_argument(
        "--capture-dir",
        type=Path,
        default=None,
        help="Write raw payloads here; omit env VOIDFIX_WEBHOOK_CAPTURE_DIR= to disable",
    )
    parser.add_argument(
        "--env-file",
        default=os.getenv("MOBI_RENT_ENV_FILE", str(ROOT / ".env")),
        help="Path to .env for SmsDispatchService wiring",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    capture_dir = args.capture_dir if args.capture_dir is not None else _env_capture_dir(ROOT)
    if capture_dir is not None:
        capture_dir.mkdir(parents=True, exist_ok=True)
    Handler.capture_dir = capture_dir
    Handler.webhook_path = args.webhook_path if args.webhook_path.startswith("/") else f"/{args.webhook_path}"

    try:
        from infrastructure.config import load_config

        cfg = load_config(env_file=args.env_file if Path(args.env_file).exists() else None)
        Handler.app_name = cfg.app_name
    except Exception:
        Handler.app_name = os.getenv("APP_NAME", "mobi-rent-agent")

    def _load_dispatch():
        from infrastructure.config import load_config

        return _load_dispatch_service_with_config(
            load_config(env_file=args.env_file if Path(args.env_file).exists() else None)
        )

    try:
        Handler.dispatch_service = _load_dispatch()
        if Handler.dispatch_service is None:
            logger.warning("SmsDispatchService unavailable; payloads will still be captured if POSTed")
        else:
            logger.info("SmsDispatchService loaded for ingest_inbound")
    except Exception as exc:
        logger.warning("Could not load SmsDispatchService: %s", exc)
        Handler.dispatch_service = None

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    logger.info(
        "listening http://%s:%s%s (health %s, capture=%s)",
        args.host,
        args.port,
        Handler.webhook_path,
        HEALTH_PATH,
        capture_dir,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
