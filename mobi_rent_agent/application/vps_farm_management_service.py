"""VPS farm management API (slots, jobs, actions, events)."""
from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
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
from application.vps_farm_inventory import (
    farm_agent_unavailable,
    mapped_farm_slots,
    offline_farm_slots,
)
from application.vps_job_worker import VpsJobWorker
from application.vps_slot_state import derive_slot_state, heartbeat_is_fresh
from infrastructure.slot_assignment_store import SlotAssignmentStore
from infrastructure.slot_event_store import SlotEventStore
from infrastructure.slot_reservation_store import SlotReservationStore
from infrastructure.slot_public_id import farm_slot_for_public_id, public_id_for_farm_slot
from infrastructure.slot_status_store import SlotStatusStore
from infrastructure.vps_job_store import VpsJobStore
from infrastructure.vps_rate_limiter import VpsRateLimiter
from application.auth_service import validate_esim_storage_key
from application.remote_access_service import rental_end_from_row
from infrastructure.esim_qr_security import extract_authoritative_esim_ref, validate_authoritative_esim_ref
from infrastructure.device_cleanup_store import DeviceCleanupStore

logger = logging.getLogger("vps_backend.farm_mgmt")

SUPPORTED_ACTIONS = {"reboot", "airplane_cycle", "voidfix_repair"}
RENTAL_ID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
# Browser/client must never name hardware identity. Reserve/assign reject these.
FORBIDDEN_CLIENT_DEVICE_KEYS = frozenset(
    {"serial", "adb_serial", "device_serial", "device_id", "udid"}
)
DEVICE_CLEANUP_EVENT = "device_cleanup_required"
DEVICE_CLEANUP_DETAIL = (
    "operator_physical_cleanup_required; no_factory_reset; no_silent_esim_delete"
)
GADS_RELEASE_OK = frozenset({200, 403, 404})


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
        reservation_store: SlotReservationStore | None = None,
        remote_access: Any = None,
        cleanup_store: DeviceCleanupStore | None = None,
        farm_task_client: Any = None,
    ) -> None:
        self._jobs = job_store
        self._assignments = assignment_store
        if reservation_store is None:
            reservation_store = SlotReservationStore(
                Path(assignment_store.db_path).with_name("slot_reservations.sqlite")
            )
        self._reservations = reservation_store
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
        self._remote_access = remote_access
        if cleanup_store is None:
            cleanup_store = DeviceCleanupStore(
                Path(assignment_store.db_path).with_name("device_cleanup.sqlite")
            )
        self._cleanup = cleanup_store
        self._farm_tasks = farm_task_client

    def set_remote_access(self, remote_access: Any) -> None:
        self._remote_access = remote_access

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

    def get_slot_status(self, slot_public_id: str, *, mapped: frozenset[int] | None = None) -> ApiResult:
        """Per-slot status derived from heartbeat + assignment + jobs (never from caller input)."""
        farm_slot = self.resolve_farm_slot(slot_public_id)
        if farm_slot is None:
            return ApiResult(404, error_body("slot_not_found"))
        physical = mapped
        if physical is None:
            farm = self._load_farm()
            if isinstance(farm, ApiResult):
                if farm_slot not in self._known_slots:
                    return ApiResult(404, error_body("slot_not_found"))
            else:
                physical = mapped_farm_slots(farm)
        if physical is not None and farm_slot not in physical:
            return ApiResult(404, error_body("slot_not_found"))
        if physical is None and farm_slot not in self._known_slots:
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
        reservation = self._reservations.get(farm_slot)
        occupied = assignment is not None or reservation is not None
        occupancy_rental = (
            assignment.rental_id
            if assignment is not None
            else (reservation.rental_id if reservation is not None else None)
        )
        occupancy_at = (
            assignment.created_at
            if assignment is not None
            else (reservation.created_at if reservation is not None else None)
        )
        active_job = self._jobs.get_active_for_slot(farm_slot)
        last_assign = self._jobs.get_latest_for_slot(farm_slot, job_type="assign")
        last_phase = provisioning_phase(last_assign) if last_assign else None
        state = derive_slot_state(
            is_assigned=occupied,
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
            "assigned": occupied,
            "rental_id": occupancy_rental,
            "assigned_at": iso_ts(occupancy_at) if occupancy_at is not None else None,
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
        """Inventory of every Farm-mapped bay (dashboard / hardware-feed)."""
        farm = self._load_farm()
        if isinstance(farm, ApiResult):
            return farm
        slots = []
        for bay in sorted(mapped_farm_slots(farm)):
            result = self.get_slot_status(public_id_for_farm_slot(bay), mapped=mapped_farm_slots(farm))
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
        farm = self._load_farm()
        if isinstance(farm, ApiResult):
            return farm
        offline = offline_farm_slots(farm)
        available: list[dict[str, Any]] = []
        for bay in sorted(mapped_farm_slots(farm)):
            if bay in offline:
                continue
            if self._assignments.is_assigned(bay):
                continue
            if self._reservations.is_reserved(bay):
                continue
            if self._slot_has_active_job(bay):
                continue
            if self._cleanup.is_required(bay):
                continue
            available.append({"bay": bay, "box": self._default_box, "slot_id": public_id_for_farm_slot(bay)})
        return ApiResult(200, {"ok": True, "available": available})

    def reserve_slot(self, bay: int, payload: dict[str, Any]) -> ApiResult:
        """Durable occupancy for a rental. Does not dispatch Farm assign/provision."""
        if bay not in self._known_slots:
            return ApiResult(404, error_body("slot_not_found"))
        if payload.get("imei2") not in (None, ""):
            return ApiResult(400, error_body("invalid_request", message="client imei2 is not accepted"))
        if any(payload.get(key) not in (None, "") for key in FORBIDDEN_CLIENT_DEVICE_KEYS):
            return ApiResult(400, error_body("invalid_request", message="client device identity is not accepted"))
        rental_id = str(payload.get("rental_id") or "").strip()
        if not RENTAL_ID_RE.match(rental_id):
            return ApiResult(400, error_body("invalid_rental_id"))
        farm = self._load_farm()
        if isinstance(farm, ApiResult):
            return farm
        if bay not in mapped_farm_slots(farm):
            return ApiResult(404, error_body("slot_not_found"))
        if self._slot_reserved_by_other(bay, rental_id):
            return ApiResult(409, error_body("slot_unavailable"))
        held = self._reservations.get(bay)
        if held is not None and held.rental_id == rental_id:
            return ApiResult(
                200,
                {
                    "ok": True,
                    "reserved": True,
                    "bay": bay,
                    "slot_id": public_id_for_farm_slot(bay),
                    "rental_id": rental_id,
                },
            )
        if self._cleanup.is_required(bay):
            return ApiResult(409, error_body("cleanup_required"))
        owner = self._resolve_reservation_owner(bay, payload, rental_id)
        if isinstance(owner, ApiResult):
            return owner
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
        reserved = self._ensure_reservation(bay, rental_id)
        if reserved is not None:
            return reserved
        logger.info("slot_reserved bay=%s", bay)
        return ApiResult(
            200,
            {
                "ok": True,
                "reserved": True,
                "bay": bay,
                "slot_id": public_id_for_farm_slot(bay),
                "rental_id": rental_id,
            },
        )

    def release_reservation(self, bay: int, rental_id: str) -> ApiResult:
        """Explicit rental-end release. Does not unclaim tenant or wipe the device."""
        if bay not in self._known_slots:
            return ApiResult(404, error_body("slot_not_found"))
        rental = str(rental_id or "").strip()
        if not RENTAL_ID_RE.match(rental):
            return ApiResult(400, error_body("invalid_rental_id"))
        held = self._reservations.get(bay)
        if held is not None and held.rental_id != rental:
            return ApiResult(409, error_body("slot_unavailable"))
        released = self._reservations.release(bay, rental)
        if released:
            self._events.append(bay, "slot_reservation_released", f"rental_id={rental}")
            logger.info("slot_reservation_released bay=%s", bay)
        return ApiResult(
            200,
            {
                "ok": True,
                "released": released,
                "bay": bay,
                "slot_id": public_id_for_farm_slot(bay),
                "rental_id": rental,
            },
        )

    def end_rental(self, rental_id: str, *, explicit: bool = False) -> ApiResult:
        """GADS → operator cleanup event → tenant unclaim → reservation release.

        Reservation is dropped only after the rental-end/unowned condition is
        confirmed. Assign/provision job completion never calls this.
        """
        rental = str(rental_id or "").strip()
        if not RENTAL_ID_RE.match(rental):
            return ApiResult(400, error_body("invalid_rental_id"))
        held = self._reservations.get_by_rental(rental)
        row = self._tenant_row_for_rental(rental)
        if isinstance(row, ApiResult):
            return row
        bay = held.farm_slot_id if held is not None else _bay_from_tenant_row(row)
        if bay is None:
            return self._ended_body(rental, bay=None, already_clear=True)
        if not self._end_condition_confirmed(row, explicit=explicit):
            return ApiResult(409, error_body("rental_active"))

        self._events.append(bay, "rental_end_started", f"rental_id={rental}")

        gads = self._release_gads(bay, rental)
        if isinstance(gads, ApiResult):
            return gads

        self._safe_customer_cleanup(bay, rental)
        self._cleanup.mark_required(bay, rental, detail=DEVICE_CLEANUP_DETAIL)
        self._events.append(bay, DEVICE_CLEANUP_EVENT, DEVICE_CLEANUP_DETAIL)

        unclaimed = self._unclaim_tenant(bay, rental)
        if isinstance(unclaimed, ApiResult):
            return unclaimed

        released = self._reservations.release(bay, rental)
        leftover = self._reservations.get_by_rental(rental)
        if leftover is not None and leftover.farm_slot_id == bay:
            logger.warning("reservation_release_incomplete bay=%s", bay)
            return ApiResult(503, error_body("auth_unavailable"))
        if released:
            self._events.append(bay, "slot_reservation_released", f"rental_id={rental}")
            logger.info("slot_reservation_released bay=%s", bay)
        return self._ended_body(rental, bay=bay, already_clear=False)

    def cancel_rental_for_customer(self, customer_id: str, rental_id: str) -> ApiResult:
        """Customer cancel: revoke GADS immediately, then fail-closed rental-end."""
        rental = str(rental_id or "").strip()
        customer = str(customer_id or "").strip()
        if not customer:
            return ApiResult(401, error_body("unauthorized"))
        if not RENTAL_ID_RE.match(rental):
            return ApiResult(404, error_body("rental_not_found"))
        row = self._tenant_row_for_rental(rental)
        if isinstance(row, ApiResult):
            return row
        if not isinstance(row, dict):
            return ApiResult(404, error_body("rental_not_found"))
        owner = str(row.get("user_id") or "").strip()
        if owner != customer:
            return ApiResult(403, error_body("rental_not_owned"))
        return self.end_rental(rental, explicit=True)

    def verify_cleanup(self, bay: int) -> ApiResult:
        """Admin-only: CLEANUP REQUIRED → AVAILABLE after physical verification."""
        if bay not in self._known_slots:
            return ApiResult(404, error_body("slot_not_found"))
        if self._reservations.is_reserved(bay) or self._assignments.is_assigned(bay):
            return ApiResult(409, error_body("slot_unavailable"))
        if self._slot_has_active_job(bay):
            return ApiResult(409, error_body("slot_unavailable"))
        if not self._cleanup.is_required(bay):
            return ApiResult(
                200,
                {
                    "ok": True,
                    "bay": bay,
                    "slot_id": public_id_for_farm_slot(bay),
                    "cleanup": "not_required",
                    "available": True,
                },
            )
        self._cleanup.clear(bay)
        self._events.append(bay, "device_cleanup_verified", "admin_verified_available")
        return ApiResult(
            200,
            {
                "ok": True,
                "bay": bay,
                "slot_id": public_id_for_farm_slot(bay),
                "cleanup": "cleared",
                "available": True,
            },
        )

    def _safe_customer_cleanup(self, bay: int, rental_id: str) -> None:
        client = self._farm_tasks
        if client is None:
            return
        try:
            result = client.run_task(
                task_type="setup_session_safe_cleanup",
                farm_slot_id=int(bay),
                payload={"rental_id": rental_id},
                job_id=str(uuid.uuid4()),
            )
            if not getattr(result, "ok", False):
                logger.warning("safe_cleanup_incomplete bay=%s", bay)
        except Exception:
            logger.warning("safe_cleanup_failed bay=%s", bay)

    def sweep_ended_rentals(self) -> None:
        """Release occupancy for expired/unowned rentals. Active rentals stay held."""
        try:
            held = self._reservations.list_all()
        except Exception:
            logger.warning("reservation_list_failed")
            return
        for item in held:
            try:
                result = self.end_rental(item.rental_id, explicit=False)
            except Exception:
                logger.warning("rental_end_sweep_failed bay=%s", item.farm_slot_id)
                continue
            if result.http_status not in (200, 404, 409):
                logger.warning("rental_end_sweep_incomplete bay=%s", item.farm_slot_id)

    def assign_slot(self, bay: int, payload: dict[str, Any]) -> ApiResult:
        if bay not in self._known_slots:
            return ApiResult(404, error_body("slot_not_found"))
        if payload.get("imei2") not in (None, ""):
            return ApiResult(400, error_body("invalid_request", message="client imei2 is not accepted"))
        if any(payload.get(key) not in (None, "") for key in FORBIDDEN_CLIENT_DEVICE_KEYS):
            return ApiResult(400, error_body("invalid_request", message="client device identity is not accepted"))
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
            self._initialize_assigned_rental(bay, rental_id)
            return ApiResult(
                202,
                assign_acceptance_body(job_id=existing_job.job_id, bay=bay, status=existing_job.status),
            )

        if self._slot_reserved_by_other(bay, rental_id):
            return ApiResult(409, error_body("slot_unavailable"))
        if self._cleanup.is_required(bay):
            return ApiResult(409, error_body("cleanup_required"))
        if self._assignments.is_assigned(bay) or self._slot_has_active_job(bay):
            return ApiResult(409, error_body("slot_unavailable"))
        farm = self._load_farm()
        if isinstance(farm, ApiResult):
            return farm
        if bay not in mapped_farm_slots(farm):
            return ApiResult(404, error_body("slot_not_found"))
        if bay in offline_farm_slots(farm):
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
        reserved = self._ensure_reservation(bay, rental_id)
        if reserved is not None:
            return reserved

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
        self._initialize_assigned_rental(bay, rental_id, customer_id=owner)
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
        farm = self._load_farm()
        if isinstance(farm, ApiResult):
            return farm
        if farm_slot not in mapped_farm_slots(farm):
            return ApiResult(404, error_body("slot_not_found"))
        if farm_slot in offline_farm_slots(farm):
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

    def _load_farm(self) -> dict[str, Any] | ApiResult:
        try:
            farm = self._farm_status()
        except (requests.RequestException, OSError, ConnectionError):
            return ApiResult(503, error_body("farm_unreachable"))
        if farm_agent_unavailable(farm):
            return ApiResult(503, error_body("farm_unreachable"))
        return farm

    def _slot_reserved_by_other(self, bay: int, rental_id: str) -> bool:
        held = self._reservations.get(bay)
        return held is not None and held.rental_id != rental_id

    def _ensure_reservation(self, bay: int, rental_id: str) -> ApiResult | None:
        existing = self._reservations.get(bay)
        if existing is not None and existing.rental_id == rental_id:
            return None
        if self._reservations.claim(bay, rental_id):
            self._events.append(bay, "slot_reserved", f"rental_id={rental_id}")
            return None
        return ApiResult(409, error_body("slot_unavailable"))

    def _resolve_reservation_owner(
        self,
        bay: int,
        payload: dict[str, Any],
        rental_id: str,
    ) -> str | ApiResult:
        if self._auth_store is None or getattr(self._auth_store, "privileged", True) is False:
            return ApiResult(503, error_body("auth_not_configured"))
        getter = getattr(self._auth_store, "get_slot_by_id", None)
        if not callable(getter):
            return ApiResult(503, error_body("auth_not_configured"))
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
        return owner

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

    def _tenant_row_for_rental(self, rental_id: str) -> dict[str, Any] | None | ApiResult:
        if self._auth_store is None:
            return None
        getter = getattr(self._auth_store, "get_slot_by_id", None)
        if not callable(getter):
            return None
        try:
            row = getter(rental_id)
        except (TypeError, ValueError, OSError, requests.RequestException):
            logger.warning("tenant_slot_id_lookup_failed")
            return ApiResult(503, error_body("auth_unavailable"))
        return row if isinstance(row, dict) else None

    def _end_condition_confirmed(self, row: dict[str, Any] | None, *, explicit: bool) -> bool:
        if explicit:
            return True
        if not row:
            return True
        if not str(row.get("user_id") or "").strip():
            return True
        ends = rental_end_from_row(row)
        return ends is not None and ends <= self._clock()

    def _initialize_assigned_rental(
        self,
        bay: int,
        rental_id: str,
        *,
        customer_id: str | None = None,
    ) -> None:
        """Start THIS rental in pre-activation. Never copies a previous rental."""
        remote = self._remote_access
        init = getattr(remote, "initialize_new_rental_state", None)
        if not callable(init):
            return
        try:
            init(rental_id=rental_id, slot_id=bay, customer_id=customer_id)
        except Exception:
            logger.warning("new_rental_state_init_failed bay=%s", bay)

    def _release_gads(self, bay: int, rental_id: str) -> ApiResult | None:
        remote = self._remote_access
        if remote is None:
            return None
        try:
            result = remote.release_device(bay, rental_id)
        except Exception:
            logger.warning("gads_release_failed bay=%s", bay)
            return ApiResult(503, error_body("remote_access_platform_error"))
        status = int(getattr(result, "http_status", 500) or 500)
        if status in GADS_RELEASE_OK:
            return None
        logger.warning("gads_release_http bay=%s", bay)
        return ApiResult(503, error_body("remote_access_platform_error"))

    def _unclaim_tenant(self, bay: int, rental_id: str) -> ApiResult | None:
        if self._auth_store is None:
            return ApiResult(503, error_body("auth_not_configured"))
        unclaim = getattr(self._auth_store, "unclaim_slot", None)
        if not callable(unclaim):
            return ApiResult(503, error_body("auth_not_configured"))
        try:
            ok = bool(unclaim(bay, rental_id))
            owner = self._auth_store.owner_of_slot(bay) if ok else "held"
        except (TypeError, ValueError, OSError, requests.RequestException):
            logger.warning("tenant_unclaim_failed bay=%s", bay)
            return ApiResult(503, error_body("auth_unavailable"))
        if not ok or owner:
            logger.warning("tenant_unclaim_incomplete bay=%s", bay)
            return ApiResult(503, error_body("auth_unavailable"))
        self._events.append(bay, "slot_unclaimed", f"rental_id={rental_id}")
        return None

    def _ended_body(self, rental_id: str, *, bay: int | None, already_clear: bool) -> ApiResult:
        body: dict[str, Any] = {
            "ok": True,
            "ended": True,
            "rental_id": rental_id,
            "gads_released": True,
            "tenant_unclaimed": True,
            "reservation_released": True,
            "device_cleanup": "not_required" if already_clear else "required",
        }
        if bay is not None:
            body["bay"] = bay
            body["slot_id"] = public_id_for_farm_slot(bay)
        return ApiResult(200, body)


def _bay_from_tenant_row(row: dict[str, Any] | None) -> int | None:
    if not isinstance(row, dict):
        return None
    for key in ("motherboard_slot_num", "bay"):
        try:
            if row.get(key) is not None:
                return int(row.get(key))
        except (TypeError, ValueError):
            continue
    return None
