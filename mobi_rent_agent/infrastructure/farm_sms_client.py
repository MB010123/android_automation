"""HTTP client: VPS → Farm Agent POST /agent/sms/send."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests

logger = logging.getLogger("vps_backend.farm_client")


@dataclass
class FarmDispatchResponse:
    ok: bool
    http_status: int
    body: dict[str, Any]
    error: str | None = None


class FarmSmsClient:
    def __init__(
        self,
        farm_agent_url: str,
        api_token: str,
        *,
        timeout_seconds: float = 120.0,
        session: requests.Session | None = None,
    ) -> None:
        self._base = farm_agent_url.rstrip("/")
        self._token = api_token
        self._timeout = timeout_seconds
        self._session = session or requests.Session()

    def send_sms(
        self,
        *,
        job_id: str,
        idempotency_key: str,
        sender_slot_id: int,
        body: str,
        to_slot_id: int | None = None,
        to_number: str | None = None,
    ) -> FarmDispatchResponse:
        url = f"{self._base}/agent/sms/send"
        payload: dict[str, Any] = {
            "job_id": job_id,
            "idempotency_key": idempotency_key,
            "sender_slot_id": sender_slot_id,
            "body": body,
        }
        if to_slot_id is not None:
            payload["to_slot_id"] = to_slot_id
        if to_number is not None:
            payload["to"] = to_number
        logger.info("farm_dispatch_attempt job_id=%s sender_slot=%s", job_id, sender_slot_id)
        try:
            response = self._session.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            logger.warning("farm_dispatch_failed job_id=%s error=%s", job_id, type(exc).__name__)
            return FarmDispatchResponse(
                ok=False,
                http_status=0,
                body={},
                error=str(exc),
            )
        try:
            body_json = response.json() if response.content else {}
        except ValueError:
            body_json = {}
        if not isinstance(body_json, dict):
            body_json = {}
        ok = 200 <= response.status_code < 300 and body_json.get("ok", response.ok)
        if ok:
            logger.info("farm_dispatch_accepted job_id=%s http=%s", job_id, response.status_code)
        return FarmDispatchResponse(
            ok=bool(ok),
            http_status=response.status_code,
            body=body_json,
            error=None if ok else body_json.get("error") or response.reason,
        )
