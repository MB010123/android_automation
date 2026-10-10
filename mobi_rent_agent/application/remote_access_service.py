"""Customer in-app remote access: authorization + orchestration.

Every public method enforces the chain

    authenticated customer_id -> rental_id -> assigned slot_id -> device_id

server-side. The browser never supplies a device id or slot id; the rental
row (Lovable/Supabase tenant authority) decides the slot and the local slot
map decides the device serial. Any failed link returns HTTP 403 and does
nothing. GADS isolation is one unique workspace per Farm-mapped bay (1–20).

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
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from application.vps_api_contract import (
    customer_inventory_identity_fields,
    customer_phone_number_for_device_status,
    error_body,
)
from application.esim_qr_upload import public_placement_success, qr_upload_error_body
from application.vps_farm_inventory import farm_agent_unavailable, mapped_farm_slots
from application.remote_access_farm_task import MAX_QR_IMAGE_BYTES, _image_extension
from application.device_display_size import DEVICE_DISPLAY_SIZE_TASK, native_resolution_body
from application.in_app_control_policy import (
    FORBIDDEN_PAYLOAD_KEYS,
    NAV_ACTIONS,
    ControlCommand,
    decide_control,
)
from application.session_restriction_policy import (
    REASON_QR_NAVIGATE,
    REASON_SETTINGS_REDIRECT,
    add_esim_public_body,
    decide_restriction,
    public_restriction_body,
)
from application.setup_activity_guard import PHASE_ESIM, PHASE_VOIDFIX
from domain.remote_access import (
    ACTIVATION_CUSTOMER_REQUIRED,
    RemoteAccessPlatform,
    RemoteAccessPlatformError,
    RemoteDeviceStatus,
    public_activation_view,
)
from infrastructure.esim_qr_security import (
    esim_fetch_url_is_public_https,
    extract_authoritative_esim_ref,
    validate_authoritative_esim_ref,
)
from infrastructure.gads_workspaces import unique_workspace_map
from infrastructure.slot_msisdn_map import SlotMsisdnMapError, load_slot_msisdn_map
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


_CUSTOMER_AUTH_ERRORS: dict[str, tuple[int, str]] = {
    "unauthenticated": (401, "unauthorized"),
    "rental_not_found": (404, "rental_not_found"),
    "rental_not_owned": (403, "rental_not_owned"),
    "rental_slot_mismatch": (403, "rental_not_owned"),
    "rental_expired": (403, "session_expired"),
    "access_expired": (403, "session_expired"),
    "access_not_active": (409, "remote_access_not_ready"),
    "poc_disabled": (503, "phone_unavailable"),
    "slot_not_allowlisted": (503, "phone_unavailable"),
    "slot_not_mapped": (503, "phone_unavailable"),
}


def _customer_denied(reason: str) -> ApiResult:
    logger.warning("remote_access_denied reason=%s", reason)
    status, code = _CUSTOMER_AUTH_ERRORS.get(reason, (403, "forbidden"))
    return ApiResult(status, error_body(code))


def _forbidden(reason: str) -> ApiResult:
    return _customer_denied(reason)


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
        prepare_slot_ids: tuple[int, ...] | None = None,
        observe_slot_ids: tuple[int, ...] | None = None,
        voidfix_android_package: str | None = None,
        session_restrictions: bool = True,
        slot_msisdn_map_path: str | None = None,
    ) -> None:
        self._enabled = bool(enabled)
        self._allowed = tuple(int(s) for s in allowed_slot_ids)
        # Full Farm/VPS serial inventory. GADS membership is workspace-per-bay,
        # not this map's key set. Unset prepare/observe follow GADS-enabled bays.
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
        self._prepare_slots = None if prepare_slot_ids is None else tuple(int(s) for s in prepare_slot_ids)
        self._observe_slots = None if observe_slot_ids is None else tuple(int(s) for s in observe_slot_ids)
        self._voidfix_package = str(voidfix_android_package or "").strip() or None
        self._session_restrictions = bool(session_restrictions)
        self._slot_msisdn_map_path = str(slot_msisdn_map_path or "").strip() or None
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
        self._slot_locks: dict[int, threading.RLock] = {}
        self._slot_locks_guard = threading.Lock()
        self._native_resolution_cache: dict[int, tuple[int, int]] = {}
        self._reconnect_cooldown = 30.0
        self._reconnect_last: dict[int, float] = {}
        self._troubleshoot_cooldown = 10.0
        self._troubleshoot_last: dict[int, float] = {}
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
        if session is None:
            return _customer_denied("access_not_active")
        if session.customer_id != auth.customer_id or session.slot_id != auth.slot_id:
            return _customer_denied("rental_not_owned")
        if not session.is_active(now):
            return ApiResult(403, error_body("session_expired"))
        if session.device_id != auth.device_id:
            return _customer_denied("slot_not_mapped")
        return session

    def _require_platform(self) -> RemoteAccessPlatform | ApiResult:
        if self._platform is None:
            return ApiResult(503, error_body("gads_unavailable"))
        return self._platform

    def _phone_readiness(self, auth: AuthorizedRental, platform: RemoteAccessPlatform) -> ApiResult | None:
        """Fail create when the phone is offline or unregistered. Never blocks on manual eSIM."""
        try:
            status = platform.device_status(
                slot_id=auth.slot_id,
                device_id=auth.device_id,
                workspace_id=self._workspace_for(auth.slot_id),
            )
        except RemoteAccessPlatformError as exc:
            logger.warning("remote_access_readiness_failed slot=%s reason=%s", auth.slot_id, exc.__class__.__name__)
            return ApiResult(502, error_body("gads_unavailable"))
        if not status.registered:
            return ApiResult(503, error_body("phone_unavailable"))
        if not status.online:
            return ApiResult(409, error_body("phone_offline"))
        return None

    def _try_voidfix_complete(self, auth: AuthorizedRental, session: RemoteAccessSession) -> dict[str, Any]:
        if not self._voidfix_package:
            return {"setup_complete": False, "voidfix_observed": "package_unconfigured"}
        inspect = self._farm_setup_task(
            "setup_session_inspect",
            auth.slot_id,
            {"phase": PHASE_VOIDFIX, "voidfix_package": self._voidfix_package, "recover": False},
        )
        if isinstance(inspect, ApiResult):
            return {"setup_complete": False, "voidfix_observed": "inspect_unavailable"}
        if not inspect.get("voidfix_is_default_sms"):
            self._store.upsert(replace(session, setup_phase=PHASE_VOIDFIX, voidfix_observed="approval_required"))
            return {"setup_complete": False, "voidfix_observed": "approval_required"}
        cycle = self._farm_setup_task(
            "setup_session_voidfix_cycle",
            auth.slot_id,
            {"voidfix_package": self._voidfix_package},
        )
        if isinstance(cycle, ApiResult):
            return {"setup_complete": False, "voidfix_observed": "verify_failed"}
        if not cycle.get("voidfix_is_default_sms") or not cycle.get("voidfix_running"):
            self._store.upsert(replace(session, setup_phase=PHASE_VOIDFIX, voidfix_observed="verify_failed"))
            return {"setup_complete": False, "voidfix_observed": "verify_failed"}
        self._store.upsert(
            replace(session, setup_phase="complete", setup_complete=True, voidfix_observed="running")
        )
        return {"setup_complete": True, "voidfix_observed": "running"}

    def _safe_qr_cleanup(self, slot_id: int) -> None:
        result = self._farm_setup_task("setup_session_safe_cleanup", slot_id, {})
        if isinstance(result, ApiResult):
            logger.warning("remote_access_safe_cleanup_skipped slot=%s", slot_id)

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
        with self._slot_op_lock(auth.slot_id):
            return self._create_remote_access_locked(auth, platform)

    def _create_remote_access_locked(self, auth: AuthorizedRental, platform: RemoteAccessPlatform) -> ApiResult:
        now = self._clock()
        other = self._store.active_for_slot(auth.slot_id, now=now)
        if other is not None and other.rental_id != auth.rental_id:
            logger.warning("remote_access_slot_busy slot=%s", auth.slot_id)
            return ApiResult(409, error_body("remote_access_busy"))

        existing = self._store.get(auth.rental_id)
        if (
            existing is not None
            and existing.is_active(now)
            and existing.customer_id == auth.customer_id
            and existing.slot_id == auth.slot_id
            and existing.device_id == auth.device_id
        ):
            logger.info("remote_access_reused slot=%s", auth.slot_id)
            body = {"ok": True, **existing.to_public_dict(now)}
            return ApiResult(200, body)

        readiness = self._phone_readiness(auth, platform)
        if isinstance(readiness, ApiResult):
            return readiness

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
            if reason == "public_url_not_https":
                return ApiResult(503, error_body("gads_unavailable"))
            return ApiResult(502, error_body("gads_unavailable"))

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
            platform_secret=grant.platform_password,
            setup_phase=existing.setup_phase if existing is not None else "esim",
            setup_complete=existing.setup_complete if existing is not None else False,
            voidfix_observed=existing.voidfix_observed if existing is not None else None,
        )
        self._store.upsert(session)
        logger.info("remote_access_created slot=%s", auth.slot_id)
        self._record(auth.slot_id, "remote_access_created", f"rental_id={auth.rental_id}")
        body = {"ok": True, **session.to_public_dict(now)}
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

    def control_session(
        self,
        customer_id: str,
        rental_id: str,
        payload: dict[str, Any] | None,
    ) -> ApiResult:
        auth = self._authorize(customer_id, rental_id, None)
        if isinstance(auth, ApiResult):
            return auth
        session = self._active_session(auth)
        if isinstance(session, ApiResult):
            return session
        decision = decide_control(payload, setup_mode=False)
        if not decision.allowed or decision.command is None:
            code = (
                decision.reason
                if decision.reason in {"invalid_control", "forbidden_control"}
                else "invalid_control"
            )
            status = 422 if code == "invalid_control" else 403
            return ApiResult(status, error_body(code))
        secret = session.platform_secret
        if not secret:
            return ApiResult(503, error_body("gads_unavailable"))
        platform = self._require_platform()
        if isinstance(platform, ApiResult):
            return platform
        command = decision.command
        restriction: dict[str, str] = {}
        try:
            with self._lock_for(auth.rental_id):
                forwarded, restriction = self._forward_restricted_control(
                    platform, session, secret, command
                )
        except RemoteAccessPlatformError as exc:
            logger.warning("in_app_control_failed slot=%s reason=%s", auth.slot_id, exc.__class__.__name__)
            return ApiResult(502, error_body("gads_unavailable"))
        if isinstance(forwarded, ApiResult):
            return forwarded
        if forwarded is False:
            return ApiResult(502, error_body("gads_unavailable"))
        body = {"ok": True, "action": decision.command.action, "forwarded": True}
        body.update(restriction)
        return ApiResult(200, body)

    def open_stream(self, customer_id: str, rental_id: str) -> ApiResult | Any:
        """Return a live MJPEG response object, or an ApiResult error."""
        auth = self._authorize(customer_id, rental_id, None)
        if isinstance(auth, ApiResult):
            return auth
        session = self._active_session(auth)
        if isinstance(session, ApiResult):
            return session
        secret = session.platform_secret
        platform = self._require_platform()
        if isinstance(platform, ApiResult):
            return platform
        opener = getattr(platform, "open_mjpeg_stream", None)
        if not callable(opener) or not secret:
            return ApiResult(503, error_body("gads_unavailable"))
        try:
            return opener(
                device_id=session.device_id,
                platform_username=session.platform_username,
                platform_password=secret,
            )
        except RemoteAccessPlatformError as exc:
            logger.warning("in_app_stream_failed slot=%s reason=%s", auth.slot_id, exc.__class__.__name__)
            return ApiResult(502, error_body("gads_unavailable"))

    def complete_setup(self, customer_id: str, rental_id: str) -> ApiResult:
        """Close the customer's remote session and run safe QR cleanup.

        Does not delete eSIM, factory-reset, or change phone security state.
        eSIM confirmation is not required to end the session.
        ``ui_state=phone_ready`` requires Farm ``ACTIVATION_CONFIRMED`` only.
        """
        auth = self._authorize(customer_id, rental_id, None)
        if isinstance(auth, ApiResult):
            return auth
        session = self._store.get(auth.rental_id)
        if session is None or session.customer_id != auth.customer_id:
            return ApiResult(409, error_body("remote_access_not_ready"))
        if session.slot_id != auth.slot_id:
            return _customer_denied("rental_not_owned")
        now = self._clock()
        setup_complete = False
        ui_state = "session_closed"
        voidfix_observed = session.voidfix_observed
        activation_observed = session.activation_observed
        if session.is_active(now):
            self._observe_activation(auth, force=True)
            session = self._store.get(auth.rental_id) or session
            activation_observed = session.activation_observed
        if str(activation_observed or "") == "confirmed":
            if session.is_active(now):
                voidfix = self._try_voidfix_complete(auth, session)
                voidfix_observed = voidfix.get("voidfix_observed")
                session = self._store.get(auth.rental_id) or session
            self._mark_phone_ready(session)
            setup_complete = True
            ui_state = "phone_ready"
        self._safe_qr_cleanup(auth.slot_id)
        ended = self._end_session(auth.rental_id, auth.slot_id, status=STATUS_REVOKED)
        body = {
            "ok": True,
            "setup_complete": setup_complete,
            "ui_state": ui_state,
            "remote_session": "closed",
            "activation_observed": activation_observed,
            "voidfix_observed": voidfix_observed,
            "esim_deleted": False,
            "factory_reset": False,
        }
        if ended.http_status not in (200, 404):
            body["ok"] = False
            body["error"] = "gads_unavailable"
            return ApiResult(502, body)
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
        with self._slot_op_lock(slot):
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
            return ApiResult(502, error_body("gads_unavailable"))
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
        platform_result = self.get_device_status(auth.slot_id)
        if platform_result.http_status != 200:
            return platform_result
        now = self._clock()
        other = self._store.active_for_slot(auth.slot_id, now=now)
        own = self._store.get(auth.rental_id)
        busy = other is not None and other.rental_id != auth.rental_id
        own_active = own is not None and own.is_active(now)
        base = dict(platform_result.body)
        adb_online = base.get("adb_online")
        gads_online = bool(base.get("online"))
        registered_state = str(base.get("state") or "")
        if adb_online is False and not gads_online:
            state = "offline"
        elif registered_state == "unregistered":
            state = "unavailable"
        elif gads_online or adb_online is True:
            state = "online"
        else:
            state = "offline"
        requires_manual = False
        if own is not None:
            view = public_activation_view(
                prepare_state=own.prepare_state,
                activation_observed=own.activation_observed,
            )
            requires_manual = view.get("activation_state") == ACTIVATION_CUSTOMER_REQUIRED
        body = {
            "ok": True,
            "state": state,
            "online": state == "online",
            "adb_online": adb_online,
            "remote_access_available": state == "online" and not busy,
            "remote_access_busy": busy,
            "requires_manual_action": requires_manual,
            "session_active": own_active,
            "busy": busy,
            "available": state == "online" and not busy,
            "slot_id": auth.slot_id,
            "coordinate_space": "native_device_pixels",
        }
        native, unavailable = self._native_resolution_for_slot(auth.slot_id)
        if native is not None:
            body["native_resolution"] = native_resolution_body(native[0], native[1])
        else:
            body["native_resolution_unavailable"] = unavailable or "wm_size_unreported"
        body.update(self._inventory_identity_for_authorized_rental(auth))
        return ApiResult(200, body)

    def reconnect_cellular_for_customer(
        self,
        customer_id: str,
        rental_id: str,
        payload: dict[str, Any] | None = None,
    ) -> ApiResult:
        """Semantic Reconnect Cellular for the owned rental Pixel only.

        Wires to the existing Farm ``airplane_cycle`` task. That task is a
        documented stub (no ``mobi_rent.network`` airplane command, no raw
        ``settings`` API). This method never reports success.
        """
        auth = self._authorize(customer_id, rental_id)
        if isinstance(auth, ApiResult):
            return auth
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            return ApiResult(422, error_body("invalid_request"))
        for key in payload:
            if str(key) in FORBIDDEN_PAYLOAD_KEYS:
                return ApiResult(403, error_body("forbidden_control"))
        lock = self._slot_lock(auth.slot_id)
        if not lock.acquire(blocking=False):
            return ApiResult(409, error_body("remote_access_busy"))
        try:
            now = float(self._clock())
            last = self._reconnect_last.get(auth.slot_id)
            if last is not None and (now - last) < self._reconnect_cooldown:
                retry_after = max(1, int(self._reconnect_cooldown - (now - last) + 0.999))
                body = error_body("rate_limited")
                body["retry_after"] = retry_after
                return ApiResult(429, body)
            if self._farm is None:
                return ApiResult(503, error_body("farm_unreachable"))
            job_id = str(uuid.uuid4())
            try:
                response = self._farm.run_task(
                    task_type="airplane_cycle",
                    farm_slot_id=auth.slot_id,
                    payload={},
                    job_id=job_id,
                )
            except Exception:  # noqa: BLE001
                logger.warning("reconnect_cellular_timeout slot=%s", auth.slot_id)
                self._reconnect_last[auth.slot_id] = now
                return ApiResult(504, error_body("timeout"))
            self._reconnect_last[auth.slot_id] = now
            err = str(response.error or "").lower()
            if response.http_status in {0, 504} or "timeout" in err:
                return ApiResult(504, error_body("timeout"))
            if (
                not response.ok
                and response.http_status != 501
                and response.error != "action_not_supported"
            ):
                return ApiResult(503, error_body("farm_unreachable"))
            # Farm Agent airplane_cycle is unsupported. Do not remap ok=True
            # into a customer success — that would fake the capability.
            logger.info("reconnect_cellular_unsupported slot=%s", auth.slot_id)
            return ApiResult(501, error_body("action_not_supported"))
        finally:
            lock.release()

    def troubleshoot_for_customer(
        self,
        customer_id: str,
        rental_id: str,
        payload: dict[str, Any] | None = None,
    ) -> ApiResult:
        """Diagnostics-first Troubleshoot Phone for the owned rental Pixel.

        Reads existing device/activation status. Optional ``action=reboot`` uses
        the allowlisted Farm ``reboot`` task only. Never airplane-cycle, shell,
        eSIM, or VoidFix.
        """
        auth = self._authorize(customer_id, rental_id)
        if isinstance(auth, ApiResult):
            return auth
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            return ApiResult(422, error_body("invalid_request"))
        for key in payload:
            if str(key) in FORBIDDEN_PAYLOAD_KEYS:
                return ApiResult(403, error_body("forbidden_control"))
        extra = {str(k) for k in payload if str(k) != "action"}
        if extra:
            return ApiResult(422, error_body("invalid_request"))
        action = str(payload.get("action") or "diagnose").strip().lower()
        if action in {"", "diagnose", "diagnostics", "status"}:
            action = "diagnose"
        if action not in {"diagnose", "reboot"}:
            if action in {"airplane", "airplane_cycle", "reconnect", "reconnect-cellular", "shell", "adb"}:
                return ApiResult(403, error_body("forbidden_control"))
            return ApiResult(422, error_body("invalid_request"))
        lock = self._slot_lock(auth.slot_id)
        if not lock.acquire(blocking=False):
            return ApiResult(409, error_body("remote_access_busy"))
        try:
            now = float(self._clock())
            last = self._troubleshoot_last.get(auth.slot_id)
            if last is not None and (now - last) < self._troubleshoot_cooldown:
                retry_after = max(1, int(self._troubleshoot_cooldown - (now - last) + 0.999))
                body = error_body("rate_limited")
                body["retry_after"] = retry_after
                return ApiResult(429, body)
            if action == "reboot":
                if self._farm is None:
                    return ApiResult(503, error_body("farm_unreachable"))
                try:
                    reboot = self.reboot_device(auth.slot_id, wait=False)
                except Exception:  # noqa: BLE001
                    logger.warning("troubleshoot_reboot_timeout slot=%s", auth.slot_id)
                    self._troubleshoot_last[auth.slot_id] = now
                    return ApiResult(504, error_body("timeout"))
                self._troubleshoot_last[auth.slot_id] = now
                err = ""
                if isinstance(reboot.body, dict):
                    err = str(reboot.body.get("error") or "").lower()
                if reboot.http_status in {0, 504} or err == "timeout":
                    return ApiResult(504, error_body("timeout"))
                if reboot.http_status != 200:
                    code = "farm_unreachable"
                    if isinstance(reboot.body, dict) and reboot.body.get("error"):
                        code = str(reboot.body.get("error"))
                    if code not in {"farm_unreachable", "action_not_supported", "timeout"}:
                        code = "farm_unreachable"
                    return ApiResult(
                        reboot.http_status if reboot.http_status >= 400 else 502,
                        error_body(code),
                    )
                snapshot = self._troubleshoot_snapshot(auth)
                snapshot["recovery"] = "reboot_requested"
                snapshot["ok"] = True
                return ApiResult(202, snapshot)
            self._troubleshoot_last[auth.slot_id] = now
            return ApiResult(200, self._troubleshoot_snapshot(auth))
        finally:
            lock.release()

    def _troubleshoot_snapshot(self, auth: AuthorizedRental) -> dict[str, Any]:
        phone = self.device_status_for_customer(auth.customer_id, auth.rental_id)
        phone_body = dict(phone.body) if isinstance(phone.body, dict) else {}
        session = self._store.get(auth.rental_id)
        now = self._clock()
        if session is not None:
            public = session.to_public_dict(now)
        else:
            public = {
                "setup_phase": "esim",
                "setup_complete": False,
                "ui_state": "session_closed",
                **public_activation_view(prepare_state=None, activation_observed=None),
            }
        farm_ok = True
        if self._farm_status is not None:
            try:
                farm = self._farm_status()
                farm_ok = bool(isinstance(farm, dict) and farm.get("ok") is not False)
            except Exception:  # noqa: BLE001
                farm_ok = False
        adb_online = phone_body.get("adb_online") if phone.http_status == 200 else None
        if phone.http_status == 200:
            phone_state = {
                "state": phone_body.get("state"),
                "online": phone_body.get("online"),
                "adb_online": adb_online,
                "session_active": phone_body.get("session_active"),
                "remote_access_busy": phone_body.get("remote_access_busy"),
            }
        else:
            phone_state = {
                "state": "unavailable",
                "online": False,
                "adb_online": self._adb_online(auth.slot_id),
                "session_active": False,
                "remote_access_busy": False,
                "error": phone_body.get("error") or "gads_unavailable",
            }
        return {
            "ok": True,
            "rental_id": auth.rental_id,
            "slot_id": auth.slot_id,
            "mode": "diagnostics",
            "phone": phone_state,
            "activation_state": public.get("activation_state"),
            "setup_phase": public.get("setup_phase"),
            "setup_complete": public.get("setup_complete"),
            "ui_state": public.get("ui_state"),
            "cellular_status": "unknown",
            "farm_reachable": farm_ok and self._farm is not None,
            "supported_recovery": ["reboot"],
            "unsupported": [
                "airplane_cycle",
                "reconnect_cellular",
                "factory_reset",
                "esim_delete",
                "voidfix_repair",
                "adb_shell",
            ],
        }

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
        body = public_placement_success(details, job_id=job_id)
        body.update(self._navigate_rental_to_add_esim(auth))
        return ApiResult(200, body)

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
            if observed == "confirmed":
                confirmed = self._store.get(auth.rental_id) or session
                if confirmed is not None:
                    self._mark_phone_ready(confirmed)
                if previous != "confirmed":
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

    def _mark_phone_ready(self, session: RemoteAccessSession) -> None:
        """Persist PHONE_READY from confirmed observation. Does not touch eSIM or VoidFix."""
        phase = str(session.setup_phase or PHASE_ESIM).strip().lower()
        if phase != PHASE_VOIDFIX:
            phase = "complete"
        self._store.upsert(replace(session, setup_complete=True, setup_phase=phase))

    def _farm_setup_task(
        self,
        task_type: str,
        slot_id: int,
        payload: dict[str, Any],
    ) -> dict[str, Any] | ApiResult:
        if self._farm is None:
            return ApiResult(503, error_body("farm_unreachable"))
        try:
            response = self._farm.run_task(
                task_type=task_type,
                farm_slot_id=int(slot_id),
                payload=payload,
                job_id=str(uuid.uuid4()),
            )
        except TimeoutError:
            logger.warning("setup_session_farm_timeout slot=%s task=%s", slot_id, task_type)
            return ApiResult(504, error_body("timeout"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("setup_session_farm_failed reason=%s", exc.__class__.__name__)
            return ApiResult(503, error_body("farm_unreachable"))
        details = response.body.get("details") if isinstance(response.body, dict) else None
        if not response.ok:
            status = 504 if response.http_status == 504 else 503 if response.http_status == 503 else 502
            code = "timeout" if status == 504 else "farm_unreachable"
            return ApiResult(status, error_body(code))
        return details if isinstance(details, dict) else {}

    def _inventory_identity_for_authorized_rental(self, auth: AuthorizedRental) -> dict[str, Any]:
        """Read-only IMEI2/EID/carrier/phone from owned assigned-slot inventory.

        Uses the same rental → assigned slot mapping as EID. IMEI2 and carrier
        come from ``public.slots`` (``imei2``, ``carrier_name``). TEMPORARY:
        ``phone_number`` is the Farm ``slot_msisdn_map`` MSISDN for that
        assigned slot when present; otherwise ``public.slots.phone_number``.
        EID is still only the rental/slot row key ``eid`` when present — there
        is no ``SLOT_SAFE_FIELDS`` EID column or live reader.
        ``cellular_status`` is always ``unknown`` (no approved radio probe).
        Empty or whitespace values stay null/unknown. Never invents placeholders,
        never infers cellular from Wi-Fi/ADB, and never asks the customer for a
        slot/serial/UDID.
        """
        rows = (self._rental_row(auth.rental_id), self._tenant_slot_row(auth.slot_id))
        return customer_inventory_identity_fields(
            imei2=_first_inventory_text(rows, "imei2"),
            eid=self._eid_for_authorized_rental(auth),
            carrier=_first_inventory_text(rows, "carrier_name", "carrier"),
            phone_number=customer_phone_number_for_device_status(
                farm_msisdn=self._farm_msisdn_for_assigned_slot(auth.slot_id),
                tenant_phone=_first_inventory_text(rows, "phone_number"),
            ),
        )

    def _farm_msisdn_for_assigned_slot(self, slot_id: int) -> str | None:
        """TEMPORARY read-only Farm SMS map lookup for the authorized assigned slot."""
        path = self._slot_msisdn_map_path
        if not path:
            return None
        try:
            mapping = load_slot_msisdn_map(path)
        except (SlotMsisdnMapError, OSError, ValueError, TypeError):
            logger.warning("remote_access_slot_msisdn_map_unavailable")
            return None
        value = mapping.get(int(slot_id))
        text = str(value).strip() if value is not None else ""
        return text or None

    def _eid_for_authorized_rental(self, auth: AuthorizedRental) -> str | None:
        """Read-only EID from tenant inventory for the owned assigned slot.

        IMEI2, carrier_name, and phone_number already live on the Lovable/Supabase
        ``slots`` row. EID does not: there is no Farm Agent task, companion
        ``get_identity`` field, ``device_registry`` field, or ``SLOT_SAFE_FIELDS``
        column that collects it, and live ADB eUICC or EuiccManager reads are not
        allowlisted. If the tenant slot/rental row already stores ``eid`` from a
        prior approved write, return that value; otherwise unknown. Never invents
        a placeholder and never asks the customer for a slot/serial/UDID.
        """
        return _first_inventory_text(
            (self._rental_row(auth.rental_id), self._tenant_slot_row(auth.slot_id)),
            "eid",
        )

    def _tenant_slot_row(self, slot_id: int) -> dict[str, Any] | None:
        getter = getattr(self._tenant, "get_slot_row", None)
        if not callable(getter):
            return None
        try:
            row = getter(int(slot_id))
        except Exception:  # noqa: BLE001 - tenant transport errors stay unknown, not guessed
            logger.warning("remote_access_tenant_slot_lookup_failed slot=%s", slot_id)
            return None
        return row if isinstance(row, dict) and row else None

    def _native_resolution_for_slot(self, slot_id: int) -> tuple[tuple[int, int] | None, str | None]:
        """Physical panel size from Farm ``wm size``. Never uses the MJPEG frame size."""
        bay = int(slot_id)
        cached = self._native_resolution_cache.get(bay)
        if cached is not None:
            return cached, None
        details = self._farm_setup_task(DEVICE_DISPLAY_SIZE_TASK, bay, {})
        if isinstance(details, ApiResult):
            logger.warning("device_display_size_unavailable slot=%s", bay)
            return None, "wm_size_unavailable"
        try:
            width = int(details.get("width"))
            height = int(details.get("height"))
        except (TypeError, ValueError):
            return None, "wm_size_unreported"
        if width <= 0 or height <= 0:
            return None, "wm_size_unreported"
        size = (width, height)
        self._native_resolution_cache[bay] = size
        return size, None

    def _farm_input(self, slot_id: int, kind: str, command: Any) -> bool | ApiResult:
        payload: dict[str, Any] = {"kind": kind}
        if kind in {"tap", "swipe"}:
            payload.update({"x": command.x, "y": command.y})
        if kind == "swipe":
            payload.update({"x2": command.x2, "y2": command.y2})
            duration_ms = getattr(command, "duration_ms", None)
            if duration_ms is not None:
                payload["duration_ms"] = int(duration_ms)
        result = self._farm_setup_task("setup_session_input", slot_id, payload)
        if isinstance(result, ApiResult):
            return result
        return True

    def _inspect_session(self, slot_id: int, *, recover: bool) -> dict[str, Any] | ApiResult:
        payload: dict[str, Any] = {"phase": PHASE_ESIM, "recover": bool(recover)}
        if self._voidfix_package:
            payload["voidfix_package"] = self._voidfix_package
        return self._farm_setup_task("setup_session_inspect", slot_id, payload)

    def _launch_add_esim(self, slot_id: int) -> dict[str, Any] | ApiResult:
        """One-shot allowlisted SIM-profiles intent. Never a background recover loop."""
        return self._inspect_session(int(slot_id), recover=True)

    def _navigate_rental_to_add_esim(self, auth: AuthorizedRental) -> dict[str, str]:
        """Open Add eSIM on the authorized rental bay only.

        Used after QR placement. Flag off is a complete no-op. Farm inspect
        failure does not fail the caller (QR is already placed). Never GADS
        revoke/release, never another bay, never a recover loop.
        """
        if not self._session_restrictions:
            return {}
        launched = self._launch_add_esim(auth.slot_id)
        if isinstance(launched, ApiResult):
            logger.warning("add_esim_navigate_skipped slot=%s", auth.slot_id)
            return {}
        return add_esim_public_body(reason=REASON_QR_NAVIGATE)

    def _send_restriction_home(
        self,
        platform: RemoteAccessPlatform,
        session: RemoteAccessSession,
        secret: str,
    ) -> bool | ApiResult:
        return self._forward_control(platform, session, secret, ControlCommand(action="home"))

    def _forward_restricted_control(
        self,
        platform: RemoteAccessPlatform,
        session: RemoteAccessSession,
        secret: str,
        command: ControlCommand,
    ) -> tuple[bool | ApiResult, dict[str, str]]:
        if not self._session_restrictions:
            if command.action == "settings":
                # No GADS/Farm settings endpoint. Rollback must not 502 or launch Add eSIM.
                return True, {}
            return self._forward_control(platform, session, secret, command), {}
        if command.action == "settings":
            launched = self._launch_add_esim(session.slot_id)
            if isinstance(launched, ApiResult):
                return launched, {}
            return True, add_esim_public_body(reason=REASON_SETTINGS_REDIRECT)
        if command.action == "back":
            return self._forward_restricted_back(platform, session, secret, command)
        forwarded = self._forward_control(platform, session, secret, command)
        if forwarded is not True:
            return forwarded, {}
        if command.action not in {"tap", "swipe", "type", "quick_settings"}:
            return True, {}
        return True, self._enforce_foreground(platform, session, secret)

    def _forward_restricted_back(
        self,
        platform: RemoteAccessPlatform,
        session: RemoteAccessSession,
        secret: str,
        command: ControlCommand,
    ) -> tuple[bool | ApiResult, dict[str, str]]:
        details = self._inspect_session(session.slot_id, recover=False)
        inspect_failed = isinstance(details, ApiResult)
        activity = None if inspect_failed else (str(details.get("activity") or "").strip() or None)
        decision = decide_restriction(
            action="back",
            activity=activity,
            voidfix_package=self._voidfix_package,
            inspect_failed=inspect_failed,
        )
        forward_command = (
            ControlCommand(action="home") if decision.rewrite_action == "home" else command
        )
        forwarded = self._forward_control(platform, session, secret, forward_command)
        if forwarded is not True:
            return forwarded, public_restriction_body(decision)
        extra = public_restriction_body(decision)
        if inspect_failed or decision.rewrite_action == "home":
            return True, extra
        after = self._enforce_foreground(platform, session, secret)
        return True, after or extra

    def _enforce_foreground(
        self,
        platform: RemoteAccessPlatform,
        session: RemoteAccessSession,
        secret: str,
    ) -> dict[str, str]:
        details = self._inspect_session(session.slot_id, recover=False)
        if isinstance(details, ApiResult):
            return {}
        decision = decide_restriction(
            action="tap",
            activity=str(details.get("activity") or "").strip() or None,
            voidfix_package=self._voidfix_package,
        )
        if decision.launch_esim:
            launched = self._launch_add_esim(session.slot_id)
            if isinstance(launched, ApiResult):
                return {}
            return public_restriction_body(decision)
        if decision.send_home:
            home_sent = self._send_restriction_home(platform, session, secret)
            if home_sent is True:
                return public_restriction_body(decision)
        return {}

    def _forward_control(
        self,
        platform: RemoteAccessPlatform,
        session: RemoteAccessSession,
        secret: str,
        command: Any,
    ) -> bool | ApiResult:
        kwargs = {
            "device_id": session.device_id,
            "platform_username": session.platform_username,
            "platform_password": secret,
        }
        if command.action == "tap":
            tap = getattr(platform, "tap", None)
            if not callable(tap):
                return self._farm_input(session.slot_id, "tap", command)
            tap(**kwargs, x=int(command.x), y=int(command.y))
            return True
        if command.action == "type":
            type_text = getattr(platform, "type_text", None)
            if not callable(type_text):
                return False
            type_text(**kwargs, text=str(command.text))
            return True
        if command.action == "swipe":
            swipe = getattr(platform, "swipe", None)
            forwarded = False
            if callable(swipe):
                swipe_kwargs = {
                    **kwargs,
                    "x": int(command.x),
                    "y": int(command.y),
                    "x2": int(command.x2),
                    "y2": int(command.y2),
                }
                duration_ms = getattr(command, "duration_ms", None)
                if duration_ms is not None:
                    try:
                        forwarded = bool(swipe(**swipe_kwargs, duration_ms=int(duration_ms)))
                    except TypeError:
                        forwarded = bool(swipe(**swipe_kwargs))
                else:
                    forwarded = bool(swipe(**swipe_kwargs))
            if not forwarded:
                return self._farm_input(session.slot_id, "swipe", command)
            return True
        if command.action in NAV_ACTIONS:
            method = {
                "notification_shade": "press_notification_shade",
                "quick_settings": "press_quick_settings",
            }.get(command.action, f"press_{command.action}")
            press = getattr(platform, method, None)
            forwarded = False
            if callable(press):
                forwarded = bool(press(**kwargs))
            if not forwarded:
                return self._farm_input(session.slot_id, command.action, command)
            return True
        if command.action == "rotate":
            set_rotation = getattr(platform, "set_rotation", None)
            if not callable(set_rotation):
                return False
            return bool(set_rotation(**kwargs, orientation=str(command.orientation)))
        return False

    def _end_session(self, rental_id: str, slot_id: int, *, status: str) -> ApiResult:
        with self._slot_op_lock(slot_id):
            return self._end_session_locked(rental_id, slot_id, status=status)

    def _end_session_locked(self, rental_id: str, slot_id: int, *, status: str) -> ApiResult:
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

    def _slot_lock(self, slot_id: int) -> threading.RLock:
        bay = int(slot_id)
        with self._slot_locks_guard:
            lock = self._slot_locks.get(bay)
            if lock is None:
                lock = threading.RLock()
                self._slot_locks[bay] = lock
            return lock

    @contextmanager
    def _slot_op_lock(self, slot_id: int) -> Iterator[None]:
        lock = self._slot_lock(slot_id)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()

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


def _first_inventory_text(rows: tuple[Any, ...], *keys: str) -> str | None:
    """First non-empty stripped inventory value from owned rental/slot rows."""
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in keys:
            text = str(row.get(key) or "").strip()
            if text:
                return text
    return None


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
