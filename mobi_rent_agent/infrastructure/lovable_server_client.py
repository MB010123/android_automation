"""VPS → Lovable machine-to-machine HTTP client.

Tenant reads/writes go through named Lovable server endpoints, never through
PostgREST and never with SUPABASE_ANON_KEY. The machine token is sent only in
the Authorization header and is never logged or returned.
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import requests

logger = logging.getLogger("vps_backend.lovable_api")

DEFAULT_TIMEOUT = 10.0
MIN_TOKEN_LENGTH = 32
SAFE_RETRY_METHODS = frozenset({"GET", "HEAD"})
RETRYABLE_STATUS = frozenset({502, 503})
MAX_RETRIES = 2


@dataclass(frozen=True)
class LovableApiResult:
    http_status: int
    body: Any
    error: str | None = None


class LovableServerClient:
    def __init__(
        self,
        base_url: str,
        machine_token: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT,
        session: requests.Session | None = None,
    ) -> None:
        base = (base_url or "").strip().rstrip("/")
        token = (machine_token or "").strip()
        if not base:
            raise ValueError("lovable_api_url_required")
        if len(token) < MIN_TOKEN_LENGTH:
            raise ValueError("lovable_machine_token_required")
        self._base = base
        self._token = token
        self._timeout = timeout_seconds
        self._http = session or requests.Session()

    @property
    def configured(self) -> bool:
        return True

    def close(self) -> None:
        self._http.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> LovableApiResult:
        verb = method.upper()
        url = f"{self._base}{path if path.startswith('/') else '/' + path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Request-Id": str(uuid.uuid4()),
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        attempts = 1 + (MAX_RETRIES if verb in SAFE_RETRY_METHODS else 0)
        last_error = "unavailable"
        for attempt in range(attempts):
            try:
                response = self._http.request(
                    verb,
                    url,
                    json=json_body,
                    headers=headers,
                    timeout=self._timeout,
                )
            except (requests.RequestException, OSError, ConnectionError):
                logger.warning("lovable_api_unavailable method=%s path=%s", verb, path)
                last_error = "unavailable"
                if attempt + 1 >= attempts:
                    return LovableApiResult(503, None, "unavailable")
                continue
            status = response.status_code
            payload = _json_body(response)
            if 200 <= status < 300:
                return LovableApiResult(status, payload, None)
            if status in (401, 403):
                logger.warning("lovable_api_unauthorized method=%s path=%s status=%s", verb, path, status)
                return LovableApiResult(status, payload, "unauthorized")
            if status == 404:
                return LovableApiResult(status, payload, "not_found")
            if status == 409:
                return LovableApiResult(status, payload, "conflict")
            if status == 400:
                return LovableApiResult(status, payload, "invalid")
            if status == 429:
                return LovableApiResult(status, payload, "rate_limited")
            logger.warning("lovable_api_http method=%s path=%s status=%s", verb, path, status)
            if status in RETRYABLE_STATUS and verb in SAFE_RETRY_METHODS and attempt + 1 < attempts:
                last_error = "unavailable"
                continue
            if status >= 500:
                return LovableApiResult(status, payload, "unavailable")
            return LovableApiResult(status, payload, "invalid")
        return LovableApiResult(503, None, last_error)


def lovable_client_from_env(*, session: requests.Session | None = None) -> LovableServerClient | None:
    """Build the production client. Never falls back to anon or service-role keys."""
    url = (os.getenv("LOVABLE_API_URL") or "").strip()
    token = (os.getenv("VPS_TO_LOVABLE_API_TOKEN") or "").strip()
    if not url or len(token) < MIN_TOKEN_LENGTH:
        return None
    timeout = float(os.getenv("REQUEST_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT)))
    try:
        return LovableServerClient(url, token, timeout_seconds=timeout, session=session)
    except ValueError:
        return None


def _json_body(response: Any) -> Any:
    content = getattr(response, "content", None)
    if not content:
        return None
    try:
        return response.json()
    except ValueError:
        return None
