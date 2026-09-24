"""VPS farm management API (slots, jobs, actions, events)."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests

from application.vps_api_contract import (
    action_acceptance_body,
    assign_acceptance_body,
    error_body,
    iso_ts,
    job_response_body,
    provisioning_phase,
)
from application.vps_job_worker import VpsJobWorker
from application.vps_slot_state import derive_slot_state, heartbeat_is_fresh
from infrastructure.slot_assignment_store import SlotAssignmentStore
from infrastructure.slot_event_store import SlotEventStore
from infrastructure.slot_public_id import farm_slot_for_public_id, public_id_for_farm_slot
from infrastructure.slot_status_store import SlotStatusStore
from infrastructure.vps_job_store import VpsJobStore
from infrastructure.vps_rate_limiter import VpsRateLimiter

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
        status_store: SlotStatusStore | None = None,
        heartbeat_interval_seconds: float = 30.0,
        clock: Callable[[], float] | None = None,
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
        self._status_store = status_store
        self._heartbeat_interval = heartbeat_interval_seconds
        self._clock = clock or time.time

    def get_slot_status(self, slot_public_id: str) -> ApiResult:
        """Per-slot status derived from heartbeat + assignment + jobs (never from caller input)."""
        farm_slot = farm_slot_for_public_id(slot_public_id, self._overrides)
        if farm_slot is None or farm_slot not in self._known_slots:
            return ApiResult(404, error_body("slot_not_found"))
        now = self._clock()
        heartbeat = self._status_store.get(farm_slot) if self._status_store is not None else None
        fresh = heartbeat_is_fresh(
            last_checked_at=heartbeat.last_checked_at if heartbeat else None,
            farm_ok=heartbeat.farm_ok if heartbeat else False,
            now=now,
            interval_seconds=self._heartbeat_interval,
        )
        adb_online: bool | None = heartbeat.adb_online if (heartbeat and fresh) else None
        assignment = self._assignments.get(farm_slot)
        active_job = self._jobs.get_active_for_slot(farm_slot)
        last_assign = self._jobs.get_latest_for_slot(farm_slot, job_type="assign")
        last_phase = provisioning_phase(last_assign) if last_assign else None
        state = derive_slot_state(
            is_assigned=assignment is not None,
            active_job_type=active_job.type if active_job else None,
            last_assign_phase=last_phase,
            heartbeat_fresh=fresh,
            adb_online=adb_online,
        )
        if heartbeat is None:
            heartbeat_state = "none"
        elif not heartbeat.farm_ok:
            heartbeat_state = "farm_unreachable"
        elif fresh:
            heartbeat_state = "fresh"
        else:
            heartbeat_state = "stale"
        body: dict[str, Any] = {
            "ok": True,
            "slot_id": public_id_for_farm_slot(farm_slot),
            "bay": farm_slot,
            "box": self._default_box,
            "status": state,
            "assigned": assignment is not None,
            "rental_id": assignment.rental_id if assignment else None,
            "assigned_at": iso_ts(assignment.created_at) if assignment else None,
            "adb_online": adb_online,
            "last_seen_at": iso_ts(heartbeat.last_seen_at) if heartbeat else None,
            "last_checked_at": iso_ts(heartbeat.last_checked_at) if heartbeat else None,
            "heartbeat": heartbeat_state,
            "heartbeat_interval_seconds": self._heartbeat_interval,
            "active_job_id": active_job.job_id if active_job else None,
            "active_job_type": active_job.type if active_job else None,
            "last_assign_job_id": last_assign.job_id if last_assign else None,
            "provisioning_phase": last_phase,
            # The Farm health endpoint only reports ADB reachability. Radio,
            # carrier, IP and IMEI2 are not observed here; never guessed.
            "cellular_status": "unknown",
            "carrier": None,
            "imei2": None,
            "imei2_status": "unknown",
            "checked_at": iso_ts(now),
        }
        return ApiResult(200, body)

    def list_available_slots(self) -> ApiResult:
        try:
            farm = self._farm_status()
        except (requests.RequestException, OSError, ConnectionError):
            return ApiResult(503, error_body("farm_unreachable"))
        if not farm.get("ok"):
            return ApiResult(503, error_body("farm_unreachable"))
        offline = set(farm.get("offline_slots") or [])
        available: list[dict[str, Any]] = []
        for bay in sorted(self._known_slots):
            if bay in offline:
                continue
            if self._assignments.is_assigned(bay):
                continue
            if self._slot_has_active_job(bay):
                continue
            available.append({"bay": bay, "box": self._default_box, "slot_id": public_id_for_farm_slot(bay)})
        return ApiResult(200, {"ok": True, "available": available})

    def assign_slot(self, bay: int, payload: dict[str, Any]) -> ApiResult:
        if bay not in self._known_slots:
            return ApiResult(404, error_body("slot_not_found"))
        rental_id = str(payload.get("rental_id") or "").strip()
        if not RENTAL_ID_RE.match(rental_id):
            return ApiResult(400, error_body("invalid_rental_id"))
        esim_qr_url = str(payload.get("esim_qr_url") or "").strip()
        carrier = str(payload.get("carrier") or "").strip()
        if not esim_qr_url or not carrier:
            return ApiResult(400, error_body("invalid_request"))
        allowed, retry_after = self._rate.check(bay)
        if not allowed:
            body = error_body("rate_limited")
            body["retry_after"] = retry_after
            return ApiResult(429, body)

        idempotency_key = f"assign-{rental_id}"
        existing_job = self._jobs.get_by_idempotency("assign", bay, idempotency_key)
        if existing_job is not None:
            current = self._assignments.get(bay)
            if (
                current is not None
                and current.job_id == existing_job.job_id
                and current.rental_id == rental_id
            ):
                logger.info("assign_idempotent_replay bay=%s job_id=%s", bay, existing_job.job_id)
                return ApiResult(
                    202,
                    assign_acceptance_body(job_id=existing_job.job_id, bay=bay, status=existing_job.status),
                )
            return ApiResult(409, error_body("slot_unavailable"))

        if self._assignments.is_assigned(bay) or self._slot_has_active_job(bay):
            return ApiResult(409, error_body("slot_unavailable"))
        try:
            farm = self._farm_status()
        except (requests.RequestException, OSError, ConnectionError):
            return ApiResult(503, error_body("farm_unreachable"))
        if not farm.get("ok"):
            return ApiResult(503, error_body("farm_unreachable"))
        if bay in (farm.get("offline_slots") or []):
            return ApiResult(409, error_body("device_offline"))

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
            held = self._assignments.get(bay)
            if held and held.job_id == record.job_id and held.rental_id == rental_id:
                pass
            else:
                return ApiResult(409, error_body("slot_unavailable"))

        self._events.append(bay, "slot_assigned", f"rental_id={rental_id} job_id={record.job_id}")
        self._events.append(bay, "assignment_requested", f"job_id={record.job_id}")
        self._events.append(bay, "provisioning_started", f"job_id={record.job_id}")
        logger.info("assignment_created job_id=%s bay=%s", record.job_id, bay)
        self._worker.enqueue_process(record.job_id)
        return ApiResult(202, assign_acceptance_body(job_id=record.job_id, bay=bay))

    def get_job(self, job_id: str) -> ApiResult:
        record = self._jobs.get(job_id)
        if record is None:
            return ApiResult(404, error_body("job_not_found"))
        return ApiResult(200, job_response_body(record))

    def enqueue_action(self, slot_public_id: str, action: str, payload: dict[str, Any]) -> ApiResult:
        if action not in SUPPORTED_ACTIONS:
            return ApiResult(400, error_body("unsupported_action"))
        farm_slot = farm_slot_for_public_id(slot_public_id, self._overrides)
        if farm_slot is None or farm_slot not in self._known_slots:
            return ApiResult(404, error_body("slot_not_found"))
        allowed, retry_after = self._rate.check(farm_slot)
        if not allowed:
            body = error_body("rate_limited")
            body["retry_after"] = retry_after
            return ApiResult(429, body)
        idempotency_key = str(payload.get("idempotency_key") or "").strip() or None
        if idempotency_key:
            existing = self._jobs.get_by_idempotency(action, farm_slot, idempotency_key)
            if existing is not None:
                logger.info("action_idempotent_replay action=%s job_id=%s", action, existing.job_id)
                return ApiResult(
                    202,
                    action_acceptance_body(
                        job_id=existing.job_id,
                        bay=farm_slot,
                        action=action,
                        status=existing.status,
                    ),
                )
        if self._slot_has_active_job(farm_slot):
            return ApiResult(409, error_body("slot_unavailable"))
        try:
            farm = self._farm_status()
        except (requests.RequestException, OSError, ConnectionError):
            return ApiResult(503, error_body("farm_unreachable"))
        if not farm.get("ok"):
            return ApiResult(503, error_body("farm_unreachable"))
        if farm_slot in (farm.get("offline_slots") or []):
            return ApiResult(409, error_body("device_offline"))
        record = self._jobs.create(
            job_type=action,
            farm_slot_id=farm_slot,
            request_payload={},
            idempotency_key=idempotency_key,
        )
        if action == "reboot":
            self._events.append(farm_slot, "reboot_requested", f"job_id={record.job_id}")
        self._events.append(farm_slot, f"{action}_requested", f"job_id={record.job_id}")
        self._events.append(farm_slot, "action_started", f"action={action} job_id={record.job_id}")
        logger.info("action_enqueued action=%s job_id=%s bay=%s", action, record.job_id, farm_slot)
        self._worker.enqueue_process(record.job_id)
        return ApiResult(
            202,
            action_acceptance_body(job_id=record.job_id, bay=farm_slot, action=action),
        )

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
            return ApiResult(404, error_body("slot_not_found"))
        since: float | None = None
        if since_raw:
            try:
                since = float(since_raw)
            except ValueError:
                return ApiResult(400, error_body("invalid_since", message="since must be a unix timestamp"))
        try:
            limit = int(limit_raw or "50")
        except ValueError:
            return ApiResult(400, error_body("invalid_limit", message="limit must be an integer"))
        events = self._events.list_events(farm_slot, since=since, limit=limit)
        from datetime import datetime, timezone

        return ApiResult(
            200,
            {
                "ok": True,
                "slot_id": slot_public_id,
                "events": [
                    {
                        "id": e.event_id,
                        "slot_id": public_id_for_farm_slot(farm_slot),
                        "at": datetime.fromtimestamp(e.created_at, tz=timezone.utc).isoformat(),
                        "type": e.event_type,
                        "detail": e.detail,
                    }
                    for e in events
                ],
            },
        )

    def _slot_has_active_job(self, farm_slot_id: int) -> bool:
        return self._jobs.has_active_job_for_slot(farm_slot_id)
