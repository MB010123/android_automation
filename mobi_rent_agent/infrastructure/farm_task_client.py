"""VPS → Farm Agent POST /agent/tasks/run and GET /agent/jobs/{id}."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests

from infrastructure.http_pool import pooled_session

logger = logging.getLogger("vps_backend.farm_task")

DEFAULT_TIMEOUT_SECONDS = 180.0
CONNECT_TIMEOUT_SECONDS = 10.0
CONNECTION_RETRIES = 1


@dataclass
class FarmTaskResponse:
    ok: bool
    http_status: int
    body: dict[str, Any]
    error: str | None = None


def farm_http_timeout(timeout_seconds: float) -> tuple[float, float]:
    """Fast TCP connect, longer read for UI dumps / screenshots."""
    read = max(1.0, float(timeout_seconds))
    connect = min(CONNECT_TIMEOUT_SECONDS, read)
    return (connect, read)


def is_timeout_error(error: str | None) -> bool:
    text = str(error or "").strip().lower()
    if not text:
        return False
    if text in {"timeout", "farm_timeout"}:
        return True
    return "timed out" in text or "read timeout" in text or "connect timeout" in text


class FarmTaskClient:
    def __init__(
        self,
        farm_agent_url: str,
        api_token: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        session: requests.Session | None = None,
    ) -> None:
        self._base = farm_agent_url.rstrip("/")
        self._token = api_token
        self._timeout = timeout_seconds
        self._session = session or pooled_session()

    def run_task(
        self,
        *,
        task_type: str,
        farm_slot_id: int,
        payload: dict[str, Any],
        job_id: str,
        timeout_seconds: float | None = None,
    ) -> FarmTaskResponse:
        url = f"{self._base}/agent/tasks/run"
        body = {
            "job_id": job_id,
            "type": task_type,
            "farm_slot_id": int(farm_slot_id),
            "payload": payload,
        }
        timeout = farm_http_timeout(
            self._timeout if timeout_seconds is None else timeout_seconds
        )
        logger.info(
            "farm_task_attempt job_id=%s type=%s slot=%s timeout=%s",
            job_id,
            task_type,
            farm_slot_id,
            timeout,
        )
        last_error = "farm_unreachable"
        attempts = 1 + CONNECTION_RETRIES
        for attempt in range(attempts):
            try:
                response = self._session.post(
                    url,
                    json=body,
                    headers={"Authorization": f"Bearer {self._token}"},
                    timeout=timeout,
                )
            except requests.Timeout:
                logger.warning(
                    "farm_task_timeout job_id=%s type=%s slot=%s attempt=%s",
                    job_id,
                    task_type,
                    farm_slot_id,
                    attempt + 1,
                )
                return FarmTaskResponse(
                    ok=False,
                    http_status=0,
                    body={},
                    error="timeout",
                )
            except requests.RequestException as exc:
                last_error = "farm_unreachable"
                logger.warning(
                    "farm_task_connect_failed job_id=%s type=%s slot=%s attempt=%s err=%s",
                    job_id,
                    task_type,
                    farm_slot_id,
                    attempt + 1,
                    exc.__class__.__name__,
                )
                if attempt + 1 < attempts:
                    continue
                return FarmTaskResponse(
                    ok=False,
                    http_status=0,
                    body={},
                    error=last_error,
                )
            try:
                parsed = response.json() if response.content else {}
            except ValueError:
                parsed = {}
            if not isinstance(parsed, dict):
                parsed = {}
            ok = 200 <= response.status_code < 300 and parsed.get("ok", False)
            return FarmTaskResponse(
                ok=bool(ok),
                http_status=response.status_code,
                body=parsed,
                error=None if ok else parsed.get("error") or response.reason,
            )
        return FarmTaskResponse(ok=False, http_status=0, body={}, error=last_error)

    def lookup_job(self, job_id: str) -> FarmTaskResponse | None:
        """Read-only Farm Agent job cache. Never starts provision_esim."""
        text = str(job_id or "").strip()
        if not text:
            return None
        url = f"{self._base}/agent/jobs/{quote(text, safe='')}"
        logger.info("farm_job_lookup job_id=%s", text)
        try:
            response = self._session.get(
                url,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=farm_http_timeout(min(self._timeout, 15.0)),
            )
        except requests.RequestException:
            return None
        try:
            parsed = response.json() if response.content else {}
        except ValueError:
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            return None
        return FarmTaskResponse(
            ok=bool(parsed.get("ok", True)),
            http_status=200,
            body=parsed,
            error=parsed.get("error"),
        )
