"""Slot-1 remote-access proof of concept: authorization + orchestration.

Every public method enforces the chain

    authenticated customer_id -> rental_id -> assigned slot_id -> device_id

server-side. The browser never supplies a device id or slot id; the rental
row (Lovable/Supabase tenant authority) decides the slot and the local slot
map decides the device serial. Any failed link returns HTTP 403 and does
nothing.

The remote platform (GADS) only provides screen/input and a temporary
lease. Rental ownership, QR security, eSIM records and billing stay in the
existing Mobi-Rent backend. Nothing here sends ``provision_esim``, calls
EuiccManager, or changes Android policy: the customer performs the normal
Android eSIM UI flow through the remote screen.
"""
from __future__ import annotations

import base64
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from application.vps_api_contract import error_body
from application.esim_qr_upload import public_placement_success, qr_upload_error_body
from application.vps_farm_inventory import farm_agent_unavailable, mapped_farm_slots
from application.remote_access_farm_task import MAX_QR_IMAGE_BYTES, _image_extension
from domain.remote_access import (
    RemoteAccessPlatform,
    RemoteAccessPlatformError,
    RemoteDeviceStatus,
)
from infrastructure.esim_qr_security import (
    esim_fetch_url_is_public_https,
    extract_authoritative_esim_ref,
    validate_authoritative_esim_ref,
)
from infrastructure.gads_workspaces import unique_workspace_map
from infrastructure.remote_access_store import (
    STATUS_ACTIVE,
    STATUS_RELEASED,
    STATUS_REVOKED,
    RemoteAccessSession,
    RemoteAccessSessionStore,
)

logger = logging.getLogger("vps_backend.remote_access")

PREPARE_PLACING_QR = "placing_qr"
PREPARE_REBOOTING = "rebooting"
PREPARE_WAITING_ADB = "waiting_adb"
PREPARE_WAITING_PLATFORM = "waiting_platform"
PREPARE_READY = "ready"
PREPARE_FAILED = "failed"
PREPARE_IN_PROGRESS = frozenset(
    {PREPARE_PLACING_QR, PREPARE_REBOOTING, PREPARE_WAITING_ADB, PREPARE_WAITING_PLATFORM}
)

REMOTE_ACCESS_PLACE_QR_TASK = "remote_access_place_qr"
REMOTE_ACCESS_ACTIVATION_TASK = "remote_access_activation_status"

_RENTAL_END_KEYS = ("ends_at", "end_at", "rental_end", "rental_ends_at", "expires_at", "end_time")


@dataclass
class ApiResult:
    http_status: int
    body: dict[str, Any]


@dataclass(frozen=True)
class AuthorizedRental:
    customer_id: str
    rental_id: str
    slot_id: int
    device_id: str
    rental_end: float | None


def _forbidden(reason: str) -> ApiResult:
    # Reason is logged server-side only; the browser gets a uniform 403.
    logger.warning("remote_access_forbidden reason=%s", reason)
    return ApiResult(403, error_body("forbidden"))


def _parse_timestamp(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000.0 if number > 1e12 else number
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def rental_end_from_row(row: dict[str, Any]) -> float | None:
    for key in _RENTAL_END_KEYS:
        if key in row:
            parsed = _parse_timestamp(row.get(key))
            if parsed is not None:
                return parsed
    return None


class RemoteAccessService:
    def __init__(
        self,
        *,
        enabled: bool,
        allowed_slot_ids: tuple[int, ...],
        slot_device_map: dict[int, str],
        platform: RemoteAccessPlatform | None,
        store: RemoteAccessSessionStore,
        tenant_store: Any,
        farm_task_client: Any = None,
        farm_status_fetcher: Callable[[], dict[str, Any]] | None = None,
        event_recorder: Callable[[int, str, str], None] | None = None,
        esim_url_prefixes: tuple[str, ...] = (),
        session_ttl_minutes: int = 60,
        reboot_timeout_seconds: float = 180.0,
        poll_interval_seconds: float = 3.0,
        observe_cooldown_seconds: float = 20.0,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        background_runner: Callable[[Callable[[], None]], None] | None = None,
        workspace_map: dict[int, str] | None = None,
        gads_slot_ids: tuple[int, ...] | None = None,
        prepare_slot_ids: tuple[int, ...] = (1,),
        observe_slot_ids: tuple[int, ...] = (1,),
    ) -> None:
        self._enabled = bool(enabled)
        self._allowed = tuple(int(s) for s in allowed_slot_ids)
        # Full Farm/VPS serial inventory. GADS membership is workspace-per-bay,
        # not this map's key set. prepare/observe stay on their own lists.
        self._devices = {
            int(slot): str(serial).strip()
            for slot, serial in slot_device_map.items()
            if str(serial).strip()
        }
        self._workspaces = unique_workspace_map(
            {
                int(slot): str(workspace).strip()
                for slot, workspace in (workspace_map or {}).items()
                if 1 <= int(slot) <= 20 and str(workspace).strip()
            }
        )
        self._gads_slots = None if gads_slot_ids is None else tuple(int(s) for s in gads_slot_ids)
        self._prepare_slots = tuple(int(s) for s in prepare_slot_ids)
        self._observe_slots = tuple(int(s) for s in observe_slot_ids)
        self._platform = platform
        self._store = store
        self._tenant = tenant_store
        self._farm = farm_task_client
        self._farm_status = farm_status_fetcher
        self._events = event_recorder
        self._esim_url_prefixes = esim_url_prefixes
        self._ttl_minutes = max(1, int(session_ttl_minutes))
        self._reboot_timeout = float(reboot_timeout_seconds)
        self._poll_interval = max(0.0, float(poll_interval_seconds))
        self._observe_cooldown = max(0.0, float(observe_cooldown_seconds))
        self._clock = clock
        self._sleep = sleep
        self._runner = background_runner or self._thread_runner
        self._flow_locks: dict[str, threading.Lock] = {}
        self._flow_locks_guard = threading.Lock()
        reconcile = getattr(self._store, "reconcile_interrupted_prepares", None)
        if callable(reconcile):
            interrupted = reconcile()
            if interrupted:
                logger.info("remote_access_prepare_reconciled count=%s", interrupted)

    # ------------------------------------------------------------------
    # authorization chain
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def allowed_slot_ids(self) -> tuple[int, ...]:
        if self._gads_slots is not None:
            return self._gads_slots
        return tuple(sorted(self._workspaces))

    def device_for_slot(self, slot_id: int) -> str | None:
        """Slot -> platform device id when the bay has a unique GADS workspace."""
        if not self._gads_slot_allowed(slot_id):
            return None
        return self._devices.get(int(slot_id))

    def _workspace_for(self, slot_id: int) -> str | None:
        return self._workspaces.get(int(slot_id)) or None

    def _gads_slot_allowed(self, slot_id: int) -> bool:
        if not self._enabled:
            return False
        bay = int(slot_id)
        if self._gads_slots is not None and bay not in self._gads_slots:
            return False
        if not self._workspace_for(bay):
            return False
        return bool(self._devices.get(bay))

    def _slot_allowed(self, slot_id: int) -> bool:
        return self._gads_slot_allowed(slot_id)

    def _rental_row(self, rental_id: str) -> dict[str, Any] | None | ApiResult:
        getter = getattr(self._tenant, "get_slot_by_id", None)
        if not callable(getter):
            return ApiResult(503, error_body("auth_not_configured"))
        try:
            row = getter(str(rental_id))
        except Exception:  # noqa: BLE001 - tenant transport errors are all "unavailable"
            logger.warning("remote_access_tenant_lookup_failed")
            return ApiResult(503, error_body("auth_unavailable"))
        return row if isinstance(row, dict) and row else None

    def _farm_bay_is_mapped(self, bay: int) -> bool | ApiResult:
        """True when Farm health lists this bay in mapped_slots. Fail closed."""
        fetcher = self._farm_status
        if not callable(fetcher):
            return False
        try:
            farm = fetcher()
        except Exception:  # noqa: BLE001
            logger.warning("esim_qr_upload_farm_status_failed")
            return ApiResult(503, error_body("farm_unreachable"))
        if not isinstance(farm, dict) or farm_agent_unavailable(farm):
            return ApiResult(503, error_body("farm_unreachable"))
        return int(bay) in mapped_farm_slots(farm)

    def _authorize(
        self,
        customer_id: str | None,
        rental_id: str,
        slot_id: int | None = None,
        *,
        require_poc_slot: bool = True,
        feature_slots: tuple[int, ...] | None = None,
    ) -> AuthorizedRental | ApiResult:
        if not self._enabled:
            return _forbidden("poc_disabled")
        customer = str(customer_id or "").strip()
        if not customer:
            return _forbidden("unauthenticated")
        rental = str(rental_id or "").strip()
        if not rental:
            return _forbidden("rental_not_found")
        row = self._rental_row(rental)
        if isinstance(row, ApiResult):
            return row
        if row is None:
            return _forbidden("rental_not_found")
        owner = str(row.get("user_id") or "").strip()
        if not owner or owner != customer:
            return _forbidden("rental_not_owned")
        row_rental = str(row.get("rental_id") or row.get("id") or "").strip()
        if row_rental and row_rental != rental:
            return _forbidden("rental_not_owned")
        row_slot = _slot_from_row(row)
        if row_slot is None:
            return _forbidden("rental_slot_mismatch")
        if slot_id is not None and int(slot_id) != row_slot:
            return _forbidden("rental_slot_mismatch")
        # Bay-keyed authority must agree with the rental row: the slot's current
        # owner (Lovable "slot by bay") has to be this customer.
        owner_lookup = getattr(self._tenant, "owner_of_slot", None)
        if callable(owner_lookup):
            try:
                bay_owner = owner_lookup(row_slot)
            except Exception:  # noqa: BLE001
                logger.warning("remote_access_tenant_owner_lookup_failed")
                return ApiResult(503, error_body("auth_unavailable"))
            if str(bay_owner or "").strip() != customer:
                return _forbidden("rental_not_owned")
        if require_poc_slot:
            if feature_slots is not None:
                if int(row_slot) not in {int(s) for s in feature_slots}:
                    return _forbidden("slot_not_allowlisted")
                device_id = self._devices.get(row_slot)
                if not device_id:
                    return _forbidden("slot_not_mapped")
            else:
                if not self._gads_slot_allowed(row_slot):
                    return _forbidden("slot_not_allowlisted")
                device_id = self._devices.get(row_slot)
                if not device_id:
                    return _forbidden("slot_not_mapped")
        else:
            mapped = self._farm_bay_is_mapped(row_slot)
            if isinstance(mapped, ApiResult):
                return mapped
            if not mapped:
                return _forbidden("slot_not_mapped")
            device_id = ""
        rental_end = rental_end_from_row(row)
        if rental_end is not None and rental_end <= self._clock():
            if require_poc_slot:
                self._revoke_if_active_session(rental, row_slot)
            return _forbidden("rental_expired")
        return AuthorizedRental(
            customer_id=customer,
            rental_id=rental,
            slot_id=row_slot,
            device_id=device_id,
            rental_end=rental_end,
        )

    def _active_session(self, auth: AuthorizedRental) -> RemoteAccessSession | ApiResult:
        session = self._store.get(auth.rental_id)
        now = self._clock()
        if session is None or not session.is_active(now):
            return _forbidden("access_not_active")
        if session.customer_id != auth.customer_id or session.slot_id != auth.slot_id:
            return _forbidden("access_not_active")
        if session.device_id != auth.device_id:
            return _forbidden("slot_not_mapped")
        return session

    def _require_platform(self) -> RemoteAccessPlatform | ApiResult:
        if self._platform is None:
            return ApiResult(503, error_body("remote_access_not_configured"))
        return self._platform

    # ------------------------------------------------------------------
    # adapter interface
    # ------------------------------------------------------------------

    def create_remote_access(self, customer_id: str, slot_id: int | None, rental_id: str) -> ApiResult:
        auth = self._authorize(customer_id, rental_id, slot_id)
        if isinstance(auth, ApiResult):
            return auth
        platform = self._require_platform()
        if isinstance(platform, ApiResult):
            return platform
        now = self._clock()
        other = self._store.active_for_slot(auth.slot_id, now=now)
        if other is not None and other.rental_id != auth.rental_id:
            logger.warning("remote_access_slot_busy slot=%s", auth.slot_id)
            return ApiResult(409, error_body("remote_access_busy"))

        existing = self._store.get(auth.rental_id)
        if existing is not None and existing.is_active(now):
            try:
                platform.revoke_access(device_id=existing.device_id, platform_username=existing.platform_username)
            except RemoteAccessPlatformError as exc:
                logger.warning("remote_access_rotate_revoke_failed reason=%s", exc.__class__.__name__)
                return ApiResult(502, error_body("remote_access_platform_error"))

        ttl_minutes = self._ttl_minutes
        if auth.rental_end is not None:
            remaining_minutes = int((auth.rental_end - now) // 60)
            ttl_minutes = max(1, min(ttl_minutes, remaining_minutes))
        try:
            workspace_id = self._workspace_for(auth.slot_id)
            if not workspace_id:
                return _forbidden("slot_not_allowlisted")
            grant = platform.grant_access(
                device_id=auth.device_id,
                rental_id=auth.rental_id,
                ttl_minutes=ttl_minutes,
                workspace_id=workspace_id,
            )
        except RemoteAccessPlatformError as exc:
            reason = str(exc)
            logger.warning("remote_access_grant_failed slot=%s reason=%s", auth.slot_id, reason)
            if reason == "device_busy":
                return ApiResult(409, error_body("remote_access_busy"))
            if reason == "public_url_not_https":
                return ApiResult(503, error_body("remote_access_not_configured"))
            return ApiResult(502, error_body("remote_access_platform_error"))

        session = RemoteAccessSession(
            rental_id=auth.rental_id,
            customer_id=auth.customer_id,
            slot_id=auth.slot_id,
            device_id=auth.device_id,
            platform_username=grant.platform_username,
            status=STATUS_ACTIVE,
            created_at=now,
            expires_at=min(grant.expires_at, now + ttl_minutes * 60),
            prepare_state=existing.prepare_state if existing is not None else None,
            prepare_detail=existing.prepare_detail if existing is not None else None,
            prepare_job_id=existing.prepare_job_id if existing is not None else None,
            activation_observed=existing.activation_observed if existing is not None else None,
            activation_observed_at=existing.activation_observed_at if existing is not None else None,
            activation_evidence=existing.activation_evidence if existing is not None else None,
        )
        self._store.upsert(session)
        logger.info("remote_access_created slot=%s", auth.slot_id)
        self._record(auth.slot_id, "remote_access_created", f"rental_id={auth.rental_id}")
        body = {"ok": True, **session.to_public_dict(now)}
        # Shown exactly once. Temporary, per-rental, non-admin platform login.
        body["platform_login"] = {
            "url": grant.access_url,
            "username": grant.platform_username,
            "password": grant.platform_password,
            "expires_at": session.expires_at,
        }
        return ApiResult(201, body)

    def get_remote_access(self, customer_id: str, slot_id: int | None, rental_id: str) -> ApiResult:
        auth = self._authorize(customer_id, rental_id, slot_id)
        if isinstance(auth, ApiResult):
            return auth
        session = self._store.get(auth.rental_id)
        now = self._clock()
        if session is None or session.customer_id != auth.customer_id:
            return ApiResult(
                200,
                {"ok": True, "rental_id": auth.rental_id, "slot_id": auth.slot_id, "status": "none", "active": False},
            )
        evidence: dict[str, Any] = {}
        if session.is_active(now) and session.prepare_state not in PREPARE_IN_PROGRESS:
            evidence = self._observe_activation(auth, force=False)
            session = self._store.get(auth.rental_id) or session
        body = {"ok": True, **session.to_public_dict(self._clock())}
        if evidence:
            body["activation_evidence"] = evidence
        return ApiResult(200, body)

    def revoke_remote_access(self, customer_id: str, slot_id: int | None, rental_id: str) -> ApiResult:
        auth = self._authorize(customer_id, rental_id, slot_id)
        if isinstance(auth, ApiResult):
            return auth
        return self._end_session(auth.rental_id, auth.slot_id, status=STATUS_REVOKED)

    def release_device(self, slot_id: int, rental_id: str) -> ApiResult:
        """System/farm-service operation when the rental ends. No customer context."""
        if not self._enabled:
            return _forbidden("poc_disabled")
        slot = int(slot_id)
        session = self._store.get(str(rental_id))
        if session is not None and session.slot_id != slot:
            return _forbidden("rental_slot_mismatch")
        if session is None and not self._gads_slot_allowed(slot):
            return _forbidden("slot_not_allowlisted")
        device_id = self._devices.get(slot)
        if not device_id and session is not None:
            device_id = session.device_id
        if not device_id:
            return _forbidden("slot_not_mapped")
        result = self._end_session(str(rental_id), slot, status=STATUS_RELEASED)
        if result.http_status not in (200, 404):
            return result
        platform = self._platform
        released = False
        if platform is not None:
            try:
                released = bool(platform.release_device(device_id=device_id))
            except RemoteAccessPlatformError as exc:
                logger.warning("remote_access_release_failed slot=%s reason=%s", slot, exc.__class__.__name__)
        logger.info("remote_access_released slot=%s", slot)
        self._record(slot, "remote_access_released", f"rental_id={rental_id}")
        return ApiResult(200, {"ok": True, "slot_id": slot, "released": released, "status": STATUS_RELEASED})

    def get_device_status(self, slot_id: int) -> ApiResult:
        slot = int(slot_id)
        if not self._gads_slot_allowed(slot):
            return _forbidden("slot_not_allowlisted")
        device_id = self._devices.get(slot)
        if not device_id:
            return _forbidden("slot_not_mapped")
        platform = self._require_platform()
        if isinstance(platform, ApiResult):
            return platform
        try:
            status = platform.device_status(
                slot_id=slot,
                device_id=device_id,
                workspace_id=self._workspace_for(slot),
            )
        except RemoteAccessPlatformError as exc:
            logger.warning("remote_access_status_failed slot=%s reason=%s", slot, exc.__class__.__name__)
            return ApiResult(502, error_body("remote_access_platform_error"))
        body = {"ok": True, **status.to_public_dict()}
        body["adb_online"] = self._adb_online(slot)
        return ApiResult(200, body)

    def reboot_device(self, slot_id: int, *, wait: bool = True) -> ApiResult:
        """Reboot through the existing Farm Agent ADB task; never through the platform."""
        slot = int(slot_id)
        if not self._gads_slot_allowed(slot):
            return _forbidden("slot_not_allowlisted")
        if not self._devices.get(slot):
            return _forbidden("slot_not_mapped")
        if self._farm is None:
            return ApiResult(503, error_body("farm_unreachable"))
        job_id = str(uuid.uuid4())
        response = self._farm.run_task(task_type="reboot", farm_slot_id=slot, payload={}, job_id=job_id)
        if not response.ok:
            return ApiResult(502, {**error_body("action_not_supported"), "detail": response.error, "job_id": job_id})
        self._record(slot, "remote_access_reboot", f"job_id={job_id}")
        body: dict[str, Any] = {"ok": True, "slot_id": slot, "job_id": job_id, "rebooted": True}
        if wait:
            body["adb_online"] = self._wait_adb_online(slot)
            body["platform_online"] = self._wait_platform_online(slot) if body["adb_online"] else False
            body["ok"] = bool(body["adb_online"] and body["platform_online"])
            if not body["ok"]:
                body["error"] = "timeout"
        return ApiResult(200 if body["ok"] else 504, body)

    # ------------------------------------------------------------------
    # customer-facing wrappers (device routes never trust browser ids)
    # ------------------------------------------------------------------

    def device_status_for_customer(self, customer_id: str, rental_id: str) -> ApiResult:
        auth = self._authorize(customer_id, rental_id)
        if isinstance(auth, ApiResult):
            return auth
        session = self._active_session(auth)
        if isinstance(session, ApiResult):
            return session
        return self.get_device_status(auth.slot_id)

    def reboot_for_customer(self, customer_id: str, rental_id: str) -> ApiResult:
        auth = self._authorize(customer_id, rental_id)
        if isinstance(auth, ApiResult):
            return auth
        session = self._active_session(auth)
        if isinstance(session, ApiResult):
            return session
        if self._farm is None:
            return ApiResult(503, error_body("farm_unreachable"))
        self._store.set_prepare_state(auth.rental_id, PREPARE_REBOOTING, detail="customer_requested")
        self._runner(lambda: self._run_reboot_flow(auth))
        return ApiResult(202, {"ok": True, "rental_id": auth.rental_id, "slot_id": auth.slot_id, "prepare_state": PREPARE_REBOOTING})

    def prepare_esim(self, customer_id: str, rental_id: str) -> ApiResult:
        """Place the rental's validated QR image in DCIM, reboot, wait for remote control.

        The customer then installs the eSIM through the normal Android UI over
        the remote screen. No provision_esim, no EuiccManager, no policy change.
        """
        auth = self._authorize(customer_id, rental_id, feature_slots=self._prepare_slots)
        if isinstance(auth, ApiResult):
            return auth
        session = self._active_session(auth)
        if isinstance(session, ApiResult):
            return session
        if self._farm is None:
            return ApiResult(503, error_body("farm_unreachable"))
        row = self._rental_row(auth.rental_id)
        if isinstance(row, ApiResult):
            return row
        raw_ref = extract_authoritative_esim_ref(row or {})
        if not raw_ref:
            return ApiResult(503, error_body("esim_ref_unavailable"))
        qr_url = validate_authoritative_esim_ref(raw_ref, allowed_url_prefixes=self._esim_url_prefixes)
        if qr_url is None:
            return ApiResult(400, error_body("invalid_request", message="authoritative eSIM reference is not allowlisted"))
        if "://" not in qr_url or not esim_fetch_url_is_public_https(qr_url):
            return ApiResult(503, error_body("esim_ref_unavailable"))
        logger.info("esim_qr_validated slot=%s", auth.slot_id)
        with self._lock_for(auth.rental_id):
            current = self._store.get(auth.rental_id)
            if current is not None and current.prepare_state in PREPARE_IN_PROGRESS:
                return ApiResult(
                    202,
                    {
                        "ok": True,
                        "rental_id": auth.rental_id,
                        "slot_id": auth.slot_id,
                        "job_id": current.prepare_job_id,
                        "prepare_state": current.prepare_state,
                        **current.to_public_dict(self._clock()),
                    },
                )
            if current is not None and current.prepare_state == PREPARE_READY:
                return ApiResult(
                    202,
                    {
                        "ok": True,
                        "rental_id": auth.rental_id,
                        "slot_id": auth.slot_id,
                        "job_id": current.prepare_job_id,
                        "prepare_state": PREPARE_READY,
                        **current.to_public_dict(self._clock()),
                    },
                )
            job_id = str(uuid.uuid4())
            self._store.set_prepare_state(auth.rental_id, PREPARE_PLACING_QR, detail=None, job_id=job_id)
        logger.info("esim_prepare_started slot=%s", auth.slot_id)
        self._runner(lambda: self._run_prepare_flow(auth, qr_url, job_id))
        return ApiResult(
            202,
            {"ok": True, "rental_id": auth.rental_id, "slot_id": auth.slot_id, "job_id": job_id, "prepare_state": PREPARE_PLACING_QR},
        )

    def upload_esim_qr(self, customer_id: str, rental_id: str, image_bytes: bytes) -> ApiResult:
        """Place a customer-uploaded QR image on the rental's Pixel Camera.

        Does not require a GADS session. Does not call assign or prepare-esim.
        Does not provision an eSIM. Serial stays on the Farm Agent.
        """
        auth = self._authorize(customer_id, rental_id, require_poc_slot=False)
        if isinstance(auth, ApiResult):
            return self._upload_error_from(auth)
        if self._farm is None:
            return ApiResult(503, qr_upload_error_body("farm_unreachable"))
        if not image_bytes:
            return ApiResult(400, qr_upload_error_body("qr_upload_missing"))
        if len(image_bytes) > MAX_QR_IMAGE_BYTES:
            return ApiResult(413, qr_upload_error_body("qr_image_too_large"))
        if _image_extension(image_bytes) is None:
            return ApiResult(400, qr_upload_error_body("qr_not_an_image"))

        job_id = str(uuid.uuid4())
        try:
            response = self._farm.run_task(
                task_type=REMOTE_ACCESS_PLACE_QR_TASK,
                farm_slot_id=auth.slot_id,
                payload={
                    "image_base64": base64.b64encode(image_bytes).decode("ascii"),
                    "rental_id": auth.rental_id,
                },
                job_id=job_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("esim_qr_upload_farm_exception reason=%s", exc.__class__.__name__)
            return ApiResult(503, qr_upload_error_body("farm_unreachable"))
        if not _qr_placement_succeeded(response):
            error_code = str(response.error or "qr_push_failed")
            details = {}
            if isinstance(getattr(response, "body", None), dict):
                raw_details = response.body.get("details")
                if isinstance(raw_details, dict):
                    details = raw_details
                    error_code = str(details.get("error_code") or error_code)
            http_status = int(getattr(response, "http_status", 0) or 422)
            if http_status < 400:
                http_status = 422
            return ApiResult(http_status, qr_upload_error_body(error_code))
        details = {}
        if isinstance(getattr(response, "body", None), dict):
            raw_details = response.body.get("details")
            if isinstance(raw_details, dict):
                details = raw_details
        logger.info("esim_qr_uploaded slot=%s job_id=%s", auth.slot_id, job_id)
        return ApiResult(200, public_placement_success(details, job_id=job_id))

    def _upload_error_from(self, result: ApiResult) -> ApiResult:
        code = "forbidden"
        if isinstance(result.body, dict):
            code = str(result.body.get("error") or code)
        message = None
        if isinstance(result.body, dict):
            message = result.body.get("message")
        return ApiResult(result.http_status, qr_upload_error_body(code, message=message if isinstance(message, str) else None))

    def activation_status_for_customer(self, customer_id: str, rental_id: str) -> ApiResult:
        """Read-only four-layer observation. Never marks ACTIVE without CONFIRMED evidence."""
        auth = self._authorize(customer_id, rental_id, feature_slots=self._observe_slots)
        if isinstance(auth, ApiResult):
            return auth
        session = self._active_session(auth)
        if isinstance(session, ApiResult):
            return session
        if session.prepare_state in PREPARE_IN_PROGRESS:
            return ApiResult(200, {"ok": True, **session.to_public_dict(self._clock())})
        evidence = self._observe_activation(auth, force=True)
        refreshed = self._store.get(auth.rental_id) or session
        body = {"ok": True, **refreshed.to_public_dict(self._clock())}
        if evidence:
            body["activation_evidence"] = evidence
        return ApiResult(200, body)

    def _observe_activation(self, auth: AuthorizedRental, *, force: bool = False) -> dict[str, Any]:
        """Read-only Farm observation. ACTIVE only after ACTIVATION_CONFIRMED."""
        with self._lock_for(auth.rental_id):
            return self._observe_activation_locked(auth, force=force)

    def _observe_activation_locked(self, auth: AuthorizedRental, *, force: bool) -> dict[str, Any]:
        session = self._store.get(auth.rental_id)
        now = self._clock()
        if (
            not force
            and session is not None
            and session.activation_observed_at is not None
            and (now - float(session.activation_observed_at)) < self._observe_cooldown
            and session.activation_evidence
        ):
            return dict(session.activation_evidence)
        if self._farm is None:
            return dict(session.activation_evidence) if session and session.activation_evidence else {}
        logger.info("activation_observation_started slot=%s", auth.slot_id)
        job_id = str(uuid.uuid4())
        try:
            response = self._farm.run_task(
                task_type=REMOTE_ACCESS_ACTIVATION_TASK,
                farm_slot_id=auth.slot_id,
                payload={"rental_id": auth.rental_id},
                job_id=job_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("remote_access_activation_observe_failed reason=%s", exc.__class__.__name__)
            return {}
        if not response.ok or not isinstance(response.body, dict):
            logger.warning("activation_observation_failed slot=%s", auth.slot_id)
            return {}
        evidence = _public_activation_evidence(response.body.get("details"))
        verdict = str(evidence.get("verdict") or "")
        observed: str | None = None
        if verdict == "ACTIVATION_CONFIRMED":
            observed = "confirmed"
            logger.info("activation_observation_confirmed slot=%s", auth.slot_id)
        elif verdict == "ACTIVATION_PARTIAL":
            observed = "partial"
            logger.info("activation_observation_partial slot=%s", auth.slot_id)
        elif verdict:
            observed = "missing"
            logger.info("activation_observation_missing slot=%s verdict=%s", auth.slot_id, verdict)
        if observed:
            previous = session.activation_observed if session is not None else None
            self._store.set_activation_observed(
                auth.rental_id,
                observed,
                observed_at=now,
                evidence=evidence,
            )
            if observed == "confirmed" and previous != "confirmed":
                logger.info("activation_state_changed slot=%s state=ACTIVE", auth.slot_id)
        return evidence

    def sweep_stale_sessions(self) -> None:
        """Revoke GADS leases whose rental has ended or changed owner. Tenant errors skip."""
        if not self._enabled:
            return
        now = self._clock()
        for session in self._store.list_active(now=now):
            if not self._slot_allowed(session.slot_id):
                continue
            row = self._rental_row(session.rental_id)
            if isinstance(row, ApiResult):
                continue
            stale = row is None
            if isinstance(row, dict):
                owner = str(row.get("user_id") or "").strip()
                if owner and owner != session.customer_id:
                    stale = True
                end = rental_end_from_row(row)
                if end is not None and end <= now:
                    stale = True
            if stale:
                logger.warning("remote_access_stale_session_swept slot=%s", session.slot_id)
                self.release_device(session.slot_id, session.rental_id)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _end_session(self, rental_id: str, slot_id: int, *, status: str) -> ApiResult:
        session = self._store.get(rental_id)
        if session is None:
            return ApiResult(404, error_body("remote_access_not_found"))
        if session.slot_id != slot_id:
            return _forbidden("rental_slot_mismatch")
        revoked = True
        if self._platform is not None and session.status == STATUS_ACTIVE:
            try:
                revoked = bool(
                    self._platform.revoke_access(
                        device_id=session.device_id,
                        platform_username=session.platform_username,
                    )
                )
            except RemoteAccessPlatformError as exc:
                logger.warning("remote_access_revoke_failed slot=%s reason=%s", slot_id, exc.__class__.__name__)
                revoked = False
        ended = self._store.end(rental_id, status=status, now=self._clock())
        if status == STATUS_REVOKED:
            logger.info("remote_access_revoked slot=%s", slot_id)
        self._record(slot_id, f"remote_access_{status}", f"rental_id={rental_id}")
        body = {"ok": True, "platform_revoked": revoked}
        if ended is not None:
            body.update(ended.to_public_dict(self._clock()))
        return ApiResult(200, body)

    def _revoke_if_active_session(self, rental_id: str, slot_id: int) -> None:
        session = self._store.get(rental_id)
        if session is None or session.status != STATUS_ACTIVE:
            return
        try:
            self._end_session(rental_id, slot_id, status=STATUS_REVOKED)
        except Exception:  # noqa: BLE001
            logger.warning("remote_access_expire_revoke_failed slot=%s", slot_id)

    def _lock_for(self, rental_id: str) -> threading.Lock:
        with self._flow_locks_guard:
            lock = self._flow_locks.get(rental_id)
            if lock is None:
                lock = threading.Lock()
                self._flow_locks[rental_id] = lock
            return lock

    def _run_prepare_flow(self, auth: AuthorizedRental, qr_url: str, job_id: str) -> None:
        try:
            response = self._farm.run_task(
                task_type=REMOTE_ACCESS_PLACE_QR_TASK,
                farm_slot_id=auth.slot_id,
                payload={"esim_qr_url": qr_url, "rental_id": auth.rental_id},
                job_id=job_id,
            )
        except Exception as exc:  # noqa: BLE001 - background thread must record, not raise
            self._store.set_prepare_state(auth.rental_id, PREPARE_FAILED, detail=f"place_qr_exception:{exc.__class__.__name__}")
            return
        if not _qr_placement_succeeded(response):
            self._store.set_prepare_state(
                auth.rental_id,
                PREPARE_FAILED,
                detail=f"place_qr:{response.error or response.http_status}",
            )
            return
        logger.info("esim_qr_placed slot=%s", auth.slot_id)
        self._record(auth.slot_id, "remote_access_qr_placed", f"rental_id={auth.rental_id} job_id={job_id}")
        self._run_reboot_flow(auth)

    def _run_reboot_flow(self, auth: AuthorizedRental) -> None:
        self._store.set_prepare_state(auth.rental_id, PREPARE_REBOOTING)
        logger.info("device_reboot_started slot=%s", auth.slot_id)
        try:
            result = self.reboot_device(auth.slot_id, wait=False)
        except Exception as exc:  # noqa: BLE001
            self._store.set_prepare_state(auth.rental_id, PREPARE_FAILED, detail=f"reboot_exception:{exc.__class__.__name__}")
            return
        if result.http_status != 200:
            self._store.set_prepare_state(auth.rental_id, PREPARE_FAILED, detail=f"reboot:{result.body.get('error')}")
            return
        self._store.set_prepare_state(auth.rental_id, PREPARE_WAITING_ADB)
        if not self._wait_adb_online(auth.slot_id):
            self._store.set_prepare_state(auth.rental_id, PREPARE_FAILED, detail="adb_reconnect_timeout")
            return
        logger.info("device_adb_ready slot=%s", auth.slot_id)
        self._store.set_prepare_state(auth.rental_id, PREPARE_WAITING_PLATFORM)
        if not self._wait_platform_online(auth.slot_id):
            self._store.set_prepare_state(auth.rental_id, PREPARE_FAILED, detail="platform_reconnect_timeout")
            return
        logger.info("gads_ready slot=%s", auth.slot_id)
        self._store.set_prepare_state(auth.rental_id, PREPARE_READY, detail="customer_can_install_esim_manually")
        logger.info("customer_activation_required slot=%s", auth.slot_id)

    def _adb_online(self, slot_id: int) -> bool | None:
        if self._farm_status is None:
            return None
        try:
            farm = self._farm_status()
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(farm, dict) or "offline_slots" not in farm:
            # Farm Agent unreachable / not configured: unknown, not "online".
            return None
        try:
            offline = {int(s) for s in (farm.get("offline_slots") or [])}
        except (TypeError, ValueError):
            return None
        return int(slot_id) not in offline

    def _wait_adb_online(self, slot_id: int) -> bool:
        deadline = self._clock() + self._reboot_timeout
        # Give adbd time to actually go down before treating "online" as reconnected.
        went_offline = False
        while self._clock() < deadline:
            online = self._adb_online(slot_id)
            if online is False:
                went_offline = True
            elif online is True and went_offline:
                return True
            elif online is True and not went_offline and (deadline - self._clock()) < self._reboot_timeout * 0.5:
                # Never observed offline (fast reboot / coarse polling); accept online after half the window.
                return True
            self._sleep(self._poll_interval)
        return False

    def _wait_platform_online(self, slot_id: int) -> bool:
        if self._platform is None:
            return False
        device_id = self._devices.get(int(slot_id))
        if not device_id:
            return False
        deadline = self._clock() + self._reboot_timeout
        while self._clock() < deadline:
            try:
                status: RemoteDeviceStatus = self._platform.device_status(slot_id=slot_id, device_id=device_id)
                if status.online:
                    return True
            except RemoteAccessPlatformError:
                pass
            self._sleep(self._poll_interval)
        return False

    def _record(self, slot_id: int, event_type: str, detail: str) -> None:
        if self._events is None:
            return
        try:
            self._events(int(slot_id), event_type, detail)
        except Exception:  # noqa: BLE001
            logger.debug("remote_access_event_record_failed", exc_info=True)

    @staticmethod
    def _thread_runner(fn: Callable[[], None]) -> None:
        threading.Thread(target=fn, name="remote-access-flow", daemon=True).start()


def _qr_placement_succeeded(response: Any) -> bool:
    if response is None or not getattr(response, "ok", False):
        return False
    body = response.body if isinstance(getattr(response, "body", None), dict) else {}
    details = body.get("details") if isinstance(body, dict) else None
    if isinstance(details, dict) and "placed" in details:
        if details.get("placed") is not True:
            return False
        if "remote_size" in details:
            try:
                return int(details.get("remote_size") or 0) > 0
            except (TypeError, ValueError):
                return False
        return True
    message = str((body or {}).get("message") or "")
    return (not message) or message.startswith("qr_placed:")


def _public_activation_evidence(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    allowed = (
        "verdict",
        "esim_profile_present",
        "esim_enabled",
        "network_registered",
        "cellular",
        "observation_complete",
    )
    return {key: raw[key] for key in allowed if key in raw and raw[key] is not None}


def _slot_from_row(row: dict[str, Any]) -> int | None:
    for key in ("motherboard_slot_num", "bay", "farm_slot_id"):
        value = row.get(key)
        if value is None:
            continue
        try:
            slot = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= slot <= 20:
            return slot
    return None
