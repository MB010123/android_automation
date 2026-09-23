"""Physical-PC Farm Agent HTTP API (health + authenticated VPS SMS commands).

  GET  /agent/health   — ADB/slot status
  POST /agent/sms/send — VPS-originated SMS via SmsDispatchService

  python tools/farm_agent_status_server.py
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.farm_agent_tasks import execute_farm_task, parse_farm_task_body
from application.farm_sms_command import execute_farm_sms_send, parse_farm_sms_send_body
from infrastructure.config import AgentConfig
from infrastructure.farm_agent_auth import authorize_farm_request, extract_bearer_token

HEALTH_PATH = "/agent/health"
STATUS_PATH = "/agent/status"
SMS_SEND_PATH = "/agent/sms/send"
TASKS_RUN_PATH = "/agent/tasks/run"

logger = logging.getLogger("farm_agent_status")


def _adb_device_states(adb_path: str) -> dict[str, str]:
    try:
        proc = subprocess.run(
            [adb_path, "devices"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"__error__": str(exc)}
    states: dict[str, str] = {}
    for line in proc.stdout.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            states[parts[0]] = parts[1]
    return states


def build_farm_status(*, adb_path: str, slot_map: dict[int, str]) -> dict[str, Any]:
    states = _adb_device_states(adb_path)
    if "__error__" in states:
        return {
            "ok": False,
            "role": "farm",
            "error": "adb_unavailable",
            "detail": states["__error__"],
            "slot_count": len(slot_map),
        }
    online = 0
    offline_slots: list[int] = []
    for slot_id, serial in sorted(slot_map.items()):
        state = states.get(serial, "missing")
        if state == "device":
            online += 1
        else:
            offline_slots.append(slot_id)
    return {
        "ok": online == len(slot_map) and len(slot_map) > 0,
        "role": "farm",
        "slot_count": len(slot_map),
        "adb_online": online,
        "offline_slots": offline_slots,
    }


class Handler(BaseHTTPRequestHandler):
    api_token: str | None = None
    adb_path: str = "adb"
    slot_map: dict[int, str] = {}
    agent_config: AgentConfig | None = None

    def log_message(self, fmt: str, *args) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, code: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self) -> bool:
        token = extract_bearer_token(self.headers.get("Authorization"))
        return authorize_farm_request(token, self.api_token)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path not in (HEALTH_PATH, STATUS_PATH):
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        if not self._authorized():
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return
        body = build_farm_status(adb_path=self.adb_path, slot_map=self.slot_map)
        self._send_json(200, body)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in (SMS_SEND_PATH, TASKS_RUN_PATH):
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        if not self._authorized():
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8") if raw else "{}")
        except json.JSONDecodeError:
            self._send_json(400, {"ok": False, "error": "invalid JSON"})
            return
        if not isinstance(data, dict):
            self._send_json(400, {"ok": False, "error": "body must be object"})
            return
        if path == TASKS_RUN_PATH:
            try:
                task = parse_farm_task_body(data)
                result = execute_farm_task(
                    adb_path=self.adb_path,
                    slot_map=self.slot_map,
                    request=task,
                    agent_config=self.agent_config,
                )
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
                return
            body: dict[str, Any] = {
                "ok": result.ok,
                "job_id": task.job_id,
                "error": result.error,
            }
            if result.message:
                body["message"] = result.message
            self._send_json(result.http_status, body)
            return
        if self.agent_config is None:
            self._send_json(503, {"ok": False, "error": "agent not configured"})
            return
        try:
            request = parse_farm_sms_send_body(data)
            result = execute_farm_sms_send(self.agent_config, request)
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        self._send_json(
            result.http_status,
            {
                "ok": result.ok,
                "job_id": result.job_id,
                "idempotency_key": result.idempotency_key,
                "status": result.status,
                "provider_message_id": result.provider_message_id,
                "duplicate": result.duplicate,
                "error": result.error,
            },
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host",
        default=os.getenv("FARM_AGENT_LISTEN_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("FARM_AGENT_LISTEN_PORT", "8790")),
    )
    parser.add_argument(
        "--env-file",
        default=os.getenv("MOBI_RENT_ENV_FILE", str(ROOT / ".env")),
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from infrastructure.adb_slot_status import SlotMapError, load_slot_map
    from infrastructure.config import ConfigError, load_config

    env_path = Path(args.env_file)
    try:
        config = load_config(env_file=str(env_path) if env_path.exists() else None)
    except ConfigError as exc:
        logger.error("config: %s", exc)
        return 1

    token = os.getenv("FARM_AGENT_API_TOKEN") or None
    if not token:
        logger.error("FARM_AGENT_API_TOKEN is required for the farm status server")
        return 1

    map_path = Path(config.slot_map_path) if config.slot_map_path else ROOT / "slot_map.json"
    try:
        slot_map = load_slot_map(map_path)
    except SlotMapError as exc:
        logger.error("slot map: %s", exc)
        return 1

    Handler.api_token = token
    Handler.adb_path = config.adb_path
    Handler.slot_map = slot_map
    Handler.agent_config = config

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    logger.info(
        "farm agent listening http://%s:%s (health %s, sms %s, tasks %s)",
        args.host,
        args.port,
        HEALTH_PATH,
        SMS_SEND_PATH,
        TASKS_RUN_PATH,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
