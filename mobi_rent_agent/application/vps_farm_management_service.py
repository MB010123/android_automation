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
from application.auth_service import validate_esim_storage_key
from infrastructure.esim_qr_security import extract_authoritative_esim_ref, validate_authoritative_esim_ref

logger = logging.getLogger("vps_backend.farm_mgmt")

SUPPORTED_ACTIONS = {"reboot", "airplane_cycle", "voidfix_repair"}
RENTAL_ID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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
        auth_store: Any = None,
        esim_url_prefixes: tuple[str, ...] = (),
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
        self._auth_store = auth_store
        self._esim_url_prefixes = esim_url_prefixes

    def resolve_farm_slot(self, slot_ref: str) -> int | None:
        farm_slot = farm_slot_for_public_id(slot_ref, self._overrides)
        if farm_slot is not None:
            return farm_slot
        resolver = getattr(self._auth_store, "farm_slot_for_slot_id", None)
        if not callable(resolver):
            return None
        try:
            return resolver(slot_ref)
        except (TypeError, ValueError, OSError, requests.RequestException):
            logger.warning("tenant_slot_id_lookup_failed")
            return None

    def get_slot_status(self, slot_public_id: str) -> ApiResult:
        """Per-slot status derived from heartbeat + assignment + jobs (never from caller input)."""
        farm_slot = self.resolve_farm_slot(slot_public_id)
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
            "carrier_name": None,
            "imei2": None,
            "imei2_status": "unknown",
            "checked_at": iso_ts(now),
        }
        self._overlay_tenant_slot(body, farm_slot)
        return ApiResult(200, body)

    def list_all_slots(self) -> ApiResult:
        """Inventory of every configured bay (dashboard / hardware-feed)."""
        slots = []
        for bay in sorted(self._known_slots):
            result = self.get_slot_status(public_id_for_farm_slot(bay))
            if result.http_status == 200:
                slots.append(result.body)
        return ApiResult(200, {"ok": True, "slots": slots, "count": len(slots)})

    def get_slot_record(self, slot_public_id: str) -> ApiResult:
        """Lovable public.slots-shaped view. Unobserved fields stay null/unknown."""
        status = self.get_slot_status(slot_public_id)
        if status.http_status != 200:
            return status
        s = status.body
        farm_slot = self.resolve_farm_slot(slot_public_id)
        owner = None
        tenant_row: dict[str, Any] = {}
        if farm_slot is not None and self._auth_store is not None:
            owner = self._auth_store.owner_of_slot(farm_slot)
            tenant_row = self._tenant_slot_row(farm_slot)
        radio = self._tenant_radio_fields(farm_slot) if farm_slot is not None else {"carrier_name": None, "imei2": None}
        box = tenant_row.get("hardware_box_id") or s["box"]
        return ApiResult(
            200,
            {
                "ok": True,
                "slot_id": s["slot_id"],
                "motherboard_slot_num": s["bay"],
                "hardware_box_id": box,
                "user_id": owner or tenant_row.get("user_id"),
                "rental_id": s["rental_id"],
                "status": s["status"],
                "assigned": s["assigned"],
                "assigned_at": s["assigned_at"],
                "carrier_name": radio["carrier_name"],
                "phone_number": tenant_row.get("phone_number"),
                "imei2": radio["imei2"],
                "imei2_status": "known" if radio["imei2"] else "unknown",
                "last_heartbeat": s["last_seen_at"] or tenant_row.get("last_heartbeat"),
                "band_lock_setting": tenant_row.get("band_lock_setting"),
                "proxy_address": tenant_row.get("proxy_address"),
                "gateway_provider": tenant_row.get("gateway_provider"),
                "provisioning_phase": s["provisioning_phase"],
                "heartbeat": s["heartbeat"],
                "adb_online": s["adb_online"],
                "checked_at": s["checked_at"],
            },
        )

    def user_owns_public_slot(self, user_id: str, slot_public_id: str) -> bool:
        farm_slot = self.resolve_farm_slot(slot_public_id)
        if farm_slot is None or farm_slot not in self._known_slots or self._auth_store is None:
            return False
        return self._auth_store.owner_of_slot(farm_slot) == user_id

    def list_slots_for_user(self, user_id: str) -> ApiResult:
        if self._auth_store is None:
            return ApiResult(200, {"ok": True, "slots": [], "count": 0})
        slots = []
        for owned in self._auth_store.list_owned_slots(user_id):
            result = self.get_slot_status(public_id_for_farm_slot(owned.farm_slot_id))
            if result.http_status != 200:
                continue
            item = dict(result.body)
            item["last_heartbeat"] = item.get("last_seen_at") or item.get("last_heartbeat")
            radio = self._tenant_radio_fields(owned.farm_slot_id)
            item["carrier_name"] = radio["carrier_name"]
            item["imei2"] = radio["imei2"]
            item["imei2_status"] = "known" if radio["imei2"] else "unknown"
            slots.append(item)
        return ApiResult(200, {"ok": True, "slots": slots, "count": len(slots)})

    def assign_slot_by_public_id(self, slot_public_id: str, payload: dict[str, Any]) -> ApiResult:
        """eSIM provision via public slot UUID (Lovable esim_uploads.qr_code_url)."""
        farm_slot = self.resolve_farm_slot(slot_public_id)
        if farm_slot is None or farm_slot not in self._known_slots:
            return ApiResult(404, error_body("slot_not_found"))
        body = dict(payload)
        if not str(body.get("esim_qr_url") or "").strip():
            qr = str(body.get("qr_code_url") or "").strip()
            if qr:
                body["esim_qr_url"] = qr
        return self.assign_slot(farm_slot, body)

    def assign_esim_for_user(self, slot_public_id: str, payload: dict[str, Any], user_id: str) -> ApiResult:
        farm_slot = self.resolve_farm_slot(slot_public_id)
        if farm_slot is None or farm_slot not in self._known_slots:
            return ApiResult(404, error_body("slot_not_found"))
        owner = self._auth_store.owner_of_slot(farm_slot) if self._auth_store is not None else None
        if owner != user_id:
            return ApiResult(404, error_body("slot_not_found"))
        raw_ref = str(payload.get("storage_key") or payload.get("qr_code_url") or payload.get("esim_qr_url") or "")
        storage_key = validate_esim_storage_key(raw_ref, allowed_url_prefixes=self._esim_url_prefixes)
        if storage_key is None:
            return ApiResult(400, error_body("invalid_request", message="eSIM reference must be a storage key or allowlisted URL"))
        body = dict(payload)
        body["esim_qr_url"] = storage_key
        body.pop("user_id", None)
        result = self.assign_slot(farm_slot, body)
        return result

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
        if payload.get("imei2") not in (None, ""):
            return ApiResult(400, error_body("invalid_request", message="client imei2 is not accepted"))
        rental_id = str(payload.get("rental_id") or "").strip()
        if not RENTAL_ID_RE.match(rental_id):
            return ApiResult(400, error_body("invalid_rental_id"))
        allowed, retry_after = self._rate.check(bay)
        if not allowed:
            body = error_body("rate_limited")
            body["retry_after"] = retry_after
            return ApiResult(429, body)

        idempotency_key = f"assign-{rental_id}"
        existing_job = self._jobs.get_by_idempotency("assign", bay, idempotency_key)
        if self._jobs.is_replayable(existing_job):
            assert existing_job is not None
            logger.info("assign_idempotent_replay bay=%s job_id=%s", bay, existing_job.job_id)
            return ApiResult(
                202,
                assign_acceptance_body(job_id=existing_job.job_id, bay=bay, status=existing_job.status),
            )

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

        resolved = self._resolve_authoritative_assignment(bay, payload, rental_id)
        if isinstance(resolved, ApiResult):
            return resolved
        owner = resolved["user_id"]
        blocked = self._reject_occupied_tenant_slot(bay, owner)
        if blocked is not None:
            return blocked
        try:
            claimed = bool(self._auth_store.claim_slot(bay, owner, rental_id))
        except (TypeError, ValueError, OSError, requests.RequestException):
            logger.warning("tenant_claim_failed bay=%s", bay)
            return ApiResult(503, error_body("auth_unavailable"))
        if not claimed:
            return ApiResult(409, error_body("slot_unavailable"))

        safe_payload = {
            "rental_id": rental_id,
            "user_id": owner,
            "carrier": resolved["carrier"],
            "carrier_name": resolved["carrier"],
            "imei2": resolved["imei2"],
            "esim_storage_key": resolved["esim_storage_key"],
            "band_lock": str(payload.get("band_lock") or ""),
            "proxy": str(payload.get("proxy") or ""),
        }
        if resolved["esim_qr_url"]:
            safe_payload["esim_qr_url"] = resolved["esim_qr_url"]
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
        farm_slot = self.resolve_farm_slot(slot_public_id)
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
        farm_slot = self.resolve_farm_slot(slot_public_id)
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

    def _resolve_authoritative_assignment(
        self,
        bay: int,
        payload: dict[str, Any],
        rental_id: str,
    ) -> dict[str, Any] | ApiResult:
        if self._auth_store is None or getattr(self._auth_store, "privileged", True) is False:
            return ApiResult(503, error_body("auth_not_configured"))
        getter = getattr(self._auth_store, "get_slot_by_id", None)
        if not callable(getter):
            return ApiResult(503, error_body("esim_ref_unavailable"))
        try:
            row = getter(rental_id)
        except (TypeError, ValueError, OSError, requests.RequestException):
            logger.warning("tenant_slot_id_lookup_failed")
            return ApiResult(503, error_body("auth_unavailable"))
        if not isinstance(row, dict) or not row:
            return ApiResult(404, error_body("slot_not_found"))
        mapped_bay = None
        for key in ("motherboard_slot_num", "bay"):
            try:
                if row.get(key) is not None:
                    mapped_bay = int(row.get(key))
                    break
            except (TypeError, ValueError):
                mapped_bay = None
        if mapped_bay != bay:
            return ApiResult(404, error_body("slot_not_found"))
        owner = str(row.get("user_id") or "").strip()
        if not owner:
            return ApiResult(409, error_body("slot_unavailable"))
        requested = str(payload.get("user_id") or "").strip()
        if requested and requested != owner:
            return ApiResult(409, error_body("slot_unavailable"))
        carrier = _optional_text(row.get("carrier_name")) or _optional_text(row.get("carrier"))
        if not carrier:
            return ApiResult(503, error_body("esim_ref_unavailable"))
        raw_ref = extract_authoritative_esim_ref(row)
        if not raw_ref:
            return ApiResult(503, error_body("esim_ref_unavailable"))
        storage_key = validate_authoritative_esim_ref(
            raw_ref,
            allowed_url_prefixes=self._esim_url_prefixes,
        )
        if storage_key is None:
            return ApiResult(400, error_body("invalid_request", message="authoritative eSIM reference is not allowlisted"))
        if "://" not in storage_key:
            return ApiResult(503, error_body("esim_ref_unavailable"))
        esim_qr_url = storage_key
        return {
            "user_id": owner,
            "carrier": carrier,
            "imei2": _optional_text(row.get("imei2")),
            "esim_storage_key": storage_key,
            "esim_qr_url": esim_qr_url,
        }

    def _slot_has_active_job(self, farm_slot_id: int) -> bool:
        return self._jobs.has_active_job_for_slot(farm_slot_id)

    def _reject_occupied_tenant_slot(self, bay: int, requested_user_id: str) -> ApiResult | None:
        if self._auth_store is None:
            return None
        if getattr(self._auth_store, "privileged", True) is False:
            return ApiResult(503, error_body("auth_not_configured"))
        try:
            if requested_user_id:
                exists = getattr(self._auth_store, "profile_exists", None)
                if callable(exists) and not exists(requested_user_id):
                    return ApiResult(400, error_body("invalid_request", message="Unknown user_id"))
            current = self._auth_store.owner_of_slot(bay)
        except (TypeError, ValueError, OSError, requests.RequestException):
            logger.warning("tenant_ownership_lookup_failed bay=%s", bay)
            return ApiResult(503, error_body("auth_unavailable"))
        if current and current != requested_user_id:
            return ApiResult(409, error_body("slot_unavailable"))
        return None

    def _tenant_slot_row(self, farm_slot_id: int) -> dict[str, Any]:
        getter = getattr(self._auth_store, "get_slot_row", None)
        if not callable(getter):
            return {}
        try:
            row = getter(farm_slot_id)
        except (TypeError, ValueError, OSError, requests.RequestException):
            logger.warning("tenant_slot_lookup_failed bay=%s", farm_slot_id)
            return {}
        return row if isinstance(row, dict) else {}

    def _tenant_radio_fields(self, farm_slot_id: int) -> dict[str, str | None]:
        row = self._tenant_slot_row(farm_slot_id)
        carrier = _optional_text(row.get("carrier_name"))
        imei2 = _optional_text(row.get("imei2"))
        return {"carrier_name": carrier, "imei2": imei2}

    def _overlay_tenant_slot(self, body: dict[str, Any], farm_slot_id: int) -> None:
        row = self._tenant_slot_row(farm_slot_id)
        radio = self._tenant_radio_fields(farm_slot_id)
        body["carrier_name"] = radio["carrier_name"]
        body["imei2"] = radio["imei2"]
        body["imei2_status"] = "known" if radio["imei2"] else "unknown"
        if radio["carrier_name"]:
            body["carrier"] = radio["carrier_name"]
        if not row:
            return
        if row.get("phone_number"):
            body["phone_number"] = row.get("phone_number")
        if row.get("hardware_box_id"):
            body["box"] = row.get("hardware_box_id")
        if row.get("band_lock_setting"):
            body["band_lock_setting"] = row.get("band_lock_setting")
        if row.get("proxy_address"):
            body["proxy_address"] = row.get("proxy_address")
        if row.get("gateway_provider"):
            body["gateway_provider"] = row.get("gateway_provider")
        if row.get("user_id"):
            body["user_id"] = row.get("user_id")
