"""HTTP implementation of the HeartbeatTransport port.

Talks to the Supabase REST API:
    POST {SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}
    Headers: apikey / Authorization: Bearer <SUPABASE_ANON_KEY>,
             Prefer: return=representation
    Body:    {hardware_agent_token, slot_id, status}

Connection drop-outs, timeouts, and non-2xx responses are caught here and
turned into a `HeartbeatResult(success=False, ...)` so the application
layer never has to deal with `requests` exceptions directly.
"""
from __future__ import annotations

import logging

import requests

from domain.models import HeartbeatPayload, HeartbeatResult
from domain.ports import HeartbeatTransport

logger = logging.getLogger("mobi_rent_agent.api_client")


class HttpHeartbeatTransport(HeartbeatTransport):
    def __init__(self, endpoint: str, anon_key: str, timeout_seconds: float = 10.0) -> None:
        self._endpoint = endpoint
        self._timeout = timeout_seconds
        self._session = requests.Session()
        self._session.headers.update(
            {
                "apikey": anon_key,
                "Authorization": f"Bearer {anon_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Prefer": "return=representation",
            }
        )

    def send(self, payload: HeartbeatPayload) -> HeartbeatResult:
        try:
            response = self._session.post(
                self._endpoint,
                json=payload.to_dict(),
                timeout=self._timeout,
            )
        except requests.exceptions.Timeout as exc:
            return HeartbeatResult(success=False, slot_id=payload.slot_id, error=f"timeout: {exc}")
        except requests.exceptions.ConnectionError as exc:
            return HeartbeatResult(success=False, slot_id=payload.slot_id, error=f"connection_error: {exc}")
        except requests.exceptions.RequestException as exc:
            return HeartbeatResult(success=False, slot_id=payload.slot_id, error=f"request_error: {exc}")

        if 200 <= response.status_code < 300:
            return HeartbeatResult(success=True, slot_id=payload.slot_id, status_code=response.status_code)

        return HeartbeatResult(
            success=False,
            slot_id=payload.slot_id,
            status_code=response.status_code,
            error=self._safe_body(response),
        )

    @staticmethod
    def _safe_body(response: requests.Response, limit: int = 500) -> str:
        try:
            return response.text[:limit]
        except Exception:  # pragma: no cover - defensive
            return "<unreadable response body>"

    def close(self) -> None:
        self._session.close()
