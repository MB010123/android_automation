"""VPS → Farm Agent POST /agent/tasks/run."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

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
        timeout_seconds: float = 120.0,
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
