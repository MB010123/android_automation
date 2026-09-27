"""VPS → Farm Agent POST /agent/tasks/run and GET /agent/jobs/{id}."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests

logger = logging.getLogger("vps_backend.farm_task")


@dataclass
class FarmTaskResponse:
    ok: bool
    http_status: int
    body: dict[str, Any]
    error: str | None = None


class FarmTaskClient:
    def __init__(
        self,
        farm_agent_url: str,
        api_token: str,
        *,
        timeout_seconds: float = 180.0,
        session: requests.Session | None = None,
    ) -> None:
        self._base = farm_agent_url.rstrip("/")
        self._token = api_token
        self._timeout = timeout_seconds
        self._session = session or requests.Session()

    def run_task(
        self,
        *,
        task_type: str,
        farm_slot_id: int,
        payload: dict[str, Any],
        job_id: str,
    ) -> FarmTaskResponse:
        url = f"{self._base}/agent/tasks/run"
        body = {
            "job_id": job_id,
            "type": task_type,
            "farm_slot_id": farm_slot_id,
            "payload": payload,
        }
        logger.info(
            "farm_task_attempt job_id=%s type=%s slot=%s",
            job_id,
            task_type,
            farm_slot_id,
        )
        try:
            response = self._session.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            return FarmTaskResponse(
                ok=False,
                http_status=0,
                body={},
                error=str(exc),
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
                timeout=min(self._timeout, 15.0),
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
