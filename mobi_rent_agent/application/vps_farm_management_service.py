"""VPS farm management API (slots, jobs, actions, events)."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

import requests

from infrastructure.slot_public_id import farm_slot_for_public_id, public_id_for_farm_slot
from infrastructure.vps_job_store import VpsJobStore
from infrastructure.slot_assignment_store import SlotAssignmentStore
from infrastructure.slot_event_store import SlotEventStore
from infrastructure.vps_rate_limiter import VpsRateLimiter
from application.vps_job_worker import VpsJobWorker

logger = logging.getLogger("vps_backend.farm_mgmt")

SUPPORTED_ACTIONS = {"reboot", "airplane_cycle", "voidfix_repair"}
RENTAL_ID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")


@dataclass
class ApiResult:
    http_status: int
    body: dict[str, Any]


class VpsFarmManagementService:
    def __init__(
        self,
        *,
        job_store: VpsJobStore,
        assignment_store: SlotAssignmentStore,
        event_store: SlotEventStore,
        job_worker: VpsJobWorker,
        farm_status_fetcher: Callable[[], dict[str, Any]],
        known_farm_slots: set[int],
        slot_id_overrides: dict[str, int] | None = None,
        default_box: str = "POD_01",
        rate_limiter: VpsRateLimiter | None = None,
    ) -> None:
        self._jobs = job_store
        self._assignments = assignment_store
        self._events = event_store
        self._worker = job_worker
        self._farm_status = farm_status_fetcher
        self._known_slots = known_farm_slots
        self._overrides = slot_id_overrides or {}
        self._default_box = default_box
        self._rate = rate_limiter or VpsRateLimiter()

    def list_available_slots(self) -> ApiResult:
        try:
            farm = self._farm_status()
        except (requests.RequestException, OSError, ConnectionError):
            return ApiResult(503, {"error": "farm_unreachable"})
        if not farm.get("ok"):
            return ApiResult(503, {"error": "farm_unreachable"})
        offline = set(farm.get("offline_slots") or [])
        available: list[dict[str, Any]] = []
        for bay in sorted(self._known_slots):
            if bay in offline:
                continue
            if self._assignments.is_assigned(bay):
                continue
            if self._slot_has_active_job(bay):
                continue
            available.append({"bay": bay, "box": self._default_box})
        return ApiResult(200, {"available": available})

    def assign_slot(self, bay: int, payload: dict[str, Any]) -> ApiResult:
        if bay not in self._known_slots:
            return ApiResult(404, {"error": "slot_not_found"})
        rental_id = str(payload.get("rental_id") or "").strip()
        if not RENTAL_ID_RE.match(rental_id):
            return ApiResult(400, {"error": "invalid_rental_id"})
        esim_qr_url = str(payload.get("esim_qr_url") or "").strip()
        carrier = str(payload.get("carrier") or "").strip()
        if not esim_qr_url or not carrier:
            return ApiResult(400, {"error": "invalid_request"})
        allowed, retry_after = self._rate.check(bay)
        if not allowed:
            return ApiResult(429, {"error": "rate_limited", "retry_after": retry_after})
        if self._assignments.is_assigned(bay) or self._slot_has_active_job(bay):
            return ApiResult(409, {"error": "slot_unavailable"})
        try:
            farm = self._farm_status()
        except (requests.RequestException, OSError, ConnectionError):
            return ApiResult(503, {"error": "farm_unreachable"})
        if not farm.get("ok"):
            return ApiResult(503, {"error": "farm_unreachable"})
        if bay in (farm.get("offline_slots") or []):
            return ApiResult(409, {"error": "device_offline"})
        idempotency_key = f"assign-{rental_id}"
        safe_payload = {
            "rental_id": rental_id,
            "carrier": carrier,
            "band_lock": str(payload.get("band_lock") or ""),
            "proxy": str(payload.get("proxy") or ""),
            "esim_qr_url": esim_qr_url,
        }
        record = self._jobs.create(
            job_type="assign",
            farm_slot_id=bay,
            request_payload=safe_payload,
            idempotency_key=idempotency_key,
        )
        if not self._assignments.claim(bay, rental_id, record.job_id):
            return ApiResult(409, {"error": "slot_unavailable"})
        self._events.append(bay, "assignment_requested", f"job_id={record.job_id}")
        self._events.append(bay, "assignment_started", f"job_id={record.job_id}")
        self._worker.enqueue_process(record.job_id)
        return ApiResult(202, {"job_id": record.job_id})

    def get_job(self, job_id: str) -> ApiResult:
        record = self._jobs.get(job_id)
        if record is None:
            return ApiResult(404, {"error": "job_not_found"})
        state = "pending" if record.status == "pending" else record.status
        if state == "done":
            state = "done"
        return ApiResult(
            200,
            {
                "job_id": record.job_id,
                "type": record.type,
                "state": state,
                "progress": record.progress,
                "error": record.error,
            },
        )

    def enqueue_action(self, slot_public_id: str, action: str, payload: dict[str, Any]) -> ApiResult:
        if action not in SUPPORTED_ACTIONS:
            return ApiResult(400, {"error": "unsupported_action"})
        farm_slot = farm_slot_for_public_id(slot_public_id, self._overrides)
        if farm_slot is None or farm_slot not in self._known_slots:
            return ApiResult(404, {"error": "slot_not_found"})
        allowed, retry_after = self._rate.check(farm_slot)
        if not allowed:
            return ApiResult(429, {"error": "rate_limited", "retry_after": retry_after})
        if self._slot_has_active_job(farm_slot):
            return ApiResult(409, {"error": "slot_unavailable"})
        try:
            farm = self._farm_status()
        except (requests.RequestException, OSError, ConnectionError):
            return ApiResult(503, {"error": "farm_unreachable"})
        if not farm.get("ok"):
            return ApiResult(503, {"error": "farm_unreachable"})
        if farm_slot in (farm.get("offline_slots") or []):
            return ApiResult(409, {"error": "device_offline"})
        idempotency_key = str(payload.get("idempotency_key") or "").strip() or None
        record = self._jobs.create(
            job_type=action,
            farm_slot_id=farm_slot,
            request_payload={},
            idempotency_key=idempotency_key,
        )
        self._events.append(farm_slot, f"action_started", f"action={action} job_id={record.job_id}")
        self._worker.enqueue_process(record.job_id)
        return ApiResult(202, {"job_id": record.job_id})

    def record_event(self, farm_slot_id: int, event_type: str, detail: str) -> None:
        self._events.append(farm_slot_id, event_type, detail)

    def list_events(
        self,
        slot_public_id: str,
        *,
        since_raw: str | None,
        limit_raw: str | None,
    ) -> ApiResult:
        farm_slot = farm_slot_for_public_id(slot_public_id, self._overrides)
        if farm_slot is None or farm_slot not in self._known_slots:
            return ApiResult(404, {"error": "slot_not_found"})
        since: float | None = None
        if since_raw:
            try:
                since = float(since_raw)
            except ValueError:
                return ApiResult(400, {"error": "invalid_since"})
        try:
            limit = int(limit_raw or "50")
        except ValueError:
            return ApiResult(400, {"error": "invalid_limit"})
        events = self._events.list_events(farm_slot, since=since, limit=limit)
        from datetime import datetime, timezone

        return ApiResult(
            200,
            {
                "events": [
                    {
                        "at": datetime.fromtimestamp(e.created_at, tz=timezone.utc).isoformat(),
                        "type": e.event_type,
                        "detail": e.detail,
                    }
                    for e in events
                ]
            },
        )

    def _slot_has_active_job(self, farm_slot_id: int) -> bool:
        return self._jobs.has_active_job_for_slot(farm_slot_id)
