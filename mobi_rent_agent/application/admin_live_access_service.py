"""Administrator live view/control by farm bay. Never creates or mutates a rental.

Authorization is admin JWT + path slot + stored session. GADS is reached with
the existing hub admin token (no per-rental grant_access). Cleanup, assignment,
billing, eSIM, and customer remote-access sessions are untouched.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable
from uuid import UUID

from application.admin_identity import is_admin_user
from application.in_app_control_policy import (
    NAV_ACTIONS,
    ControlCommand,
    decide_control,
)
from application.vps_api_contract import error_body
from domain.remote_access import RemoteAccessPlatformError
from infrastructure.admin_live_access_store import (
    STATUS_ACTIVE,
    AdminLiveAccessSession,
    AdminLiveAccessStore,
)
from infrastructure.auth_models import AuthUser
from infrastructure.gads_remote_access import GADS_HUB_ADMIN_USERNAME

logger = logging.getLogger("vps_backend.admin_live_access")

MIN_SLOT = 1
MAX_SLOT = 20
_MAX_TTL_MINUTES = 60
_MIN_TTL_MINUTES = 5
AIRPLANE_MODE_UNSUPPORTED_MESSAGE = (
    "Airplane Mode cannot be changed through this admin live-access session. "
    "Android 16 does not allow the current Farm/GADS/companion path to toggle it."
)


@dataclass
class ApiResult:
    http_status: int
    body: dict[str, Any]


def _parse_session_id(raw: Any) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return str(UUID(text))
    except (TypeError, ValueError):
        return None


class AdminLiveAccessService:
    def __init__(
        self,
        *,
        enabled: bool,
        slot_device_map: dict[int, str],
        platform: Any,
        store: AdminLiveAccessStore,
        farm_task_client: Any = None,
        event_recorder: Callable[[int, str, str], None] | None = None,
        assignment_store: Any = None,
        cleanup_store: Any = None,
        session_ttl_minutes: int = 15,
        gads_slot_ids: tuple[int, ...] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.enabled = bool(enabled)
        self._slots = {int(k): str(v) for k, v in (slot_device_map or {}).items()}
        self._platform = platform
        self._store = store
        self._farm = farm_task_client
        self._record = event_recorder
        self._assignments = assignment_store
        self._cleanup = cleanup_store
        ttl = int(session_ttl_minutes)
        self._ttl_minutes = min(max(ttl, _MIN_TTL_MINUTES), _MAX_TTL_MINUTES)
        self._gads_slots = (
            frozenset(int(s) for s in gads_slot_ids)
            if gads_slot_ids is not None
            else None
        )
        self._clock = clock
        self._locks_guard = threading.Lock()
        self._slot_locks: dict[int, threading.Lock] = {}

    def sweep_stale_sessions(self) -> int:
        return self._store.expire_stale(now=self._clock())

    def create_session(self, admin: AuthUser, slot_id: int) -> ApiResult:
        if not is_admin_user(admin):
            return ApiResult(403, error_body("admin_forbidden"))
        parsed = self._parse_slot(slot_id)
        if isinstance(parsed, ApiResult):
            return parsed
        ready = self._require_ready(parsed)
        if isinstance(ready, ApiResult):
            return ready
        device_id, platform = ready
        with self._lock_for(parsed):
            existing = self._store.active_for_admin_slot(
                admin_user_id=admin.user_id, slot_id=parsed, now=self._clock()
            )
            if existing is not None and existing.device_id == device_id:
                logger.info(
                    "admin_live_access_reused slot=%s admin=%s",
                    parsed,
                    admin.user_id[:8],
                )
                return ApiResult(200, existing.to_public_dict(self._clock()))
            session = self._store.create(
                admin_user_id=admin.user_id,
                slot_id=parsed,
                device_id=device_id,
                ttl_seconds=self._ttl_minutes * 60,
                now=self._clock(),
            )
        self._audit(parsed, "admin_live_access_opened", admin.user_id)
        logger.info("admin_live_access_opened slot=%s admin=%s", parsed, admin.user_id[:8])
        _ = platform
        return ApiResult(201, session.to_public_dict(self._clock()))

    def open_stream(
        self, admin: AuthUser, slot_id: int, session_id: str | None
    ) -> ApiResult | Any:
        bound = self._bound_session(admin, slot_id, session_id)
        if isinstance(bound, ApiResult):
            return bound
        ready = self._require_ready(bound.slot_id)
        if isinstance(ready, ApiResult):
            return ready
        device_id, platform = ready
        if device_id != bound.device_id:
            return ApiResult(409, error_body("phone_unavailable"))
        opener = getattr(platform, "open_mjpeg_stream", None)
        if not callable(opener):
            return ApiResult(503, error_body("gads_unavailable"))
        try:
            return opener(
                device_id=bound.device_id,
                platform_username=GADS_HUB_ADMIN_USERNAME,
                platform_password="",
            )
        except RemoteAccessPlatformError:
            logger.warning("admin_live_access_stream_failed slot=%s", bound.slot_id)
            return ApiResult(502, error_body("gads_unavailable"))

    def control_session(
        self,
        admin: AuthUser,
        slot_id: int,
        payload: dict[str, Any] | None,
    ) -> ApiResult:
        raw = payload if isinstance(payload, dict) else {}
        session_id = _parse_session_id(raw.get("session_id"))
        bound = self._bound_session(admin, slot_id, session_id)
        if isinstance(bound, ApiResult):
            return bound
        control_body = {key: value for key, value in raw.items() if key != "session_id"}
        decision = decide_control(control_body, setup_mode=False)
        if not decision.allowed or decision.command is None:
            code = (
                decision.reason
                if decision.reason in {"invalid_control", "forbidden_control"}
                else "invalid_control"
            )
            status = 422 if code == "invalid_control" else 403
            return ApiResult(status, error_body(code))
        if decision.command.action == "airplane_mode":
            return ApiResult(
                501,
                error_body(
                    "action_not_supported",
                    message=AIRPLANE_MODE_UNSUPPORTED_MESSAGE,
                ),
            )
        ready = self._require_ready(bound.slot_id)
        if isinstance(ready, ApiResult):
            return ready
        device_id, platform = ready
        if device_id != bound.device_id:
            return ApiResult(409, error_body("phone_unavailable"))
        try:
            forwarded = self._forward_control(platform, bound, decision.command)
        except RemoteAccessPlatformError:
            logger.warning("admin_live_access_control_failed slot=%s", bound.slot_id)
            return ApiResult(502, error_body("gads_unavailable"))
        if isinstance(forwarded, ApiResult):
            return forwarded
        if forwarded is False:
            return ApiResult(502, error_body("gads_unavailable"))
        return ApiResult(
            200,
            {
                "ok": True,
                "action": decision.command.action,
                "forwarded": True,
                "slot_id": bound.slot_id,
                "session_id": bound.session_id,
                "rental_unchanged": True,
                "cleanup_unchanged": True,
            },
        )

    def close_session(
        self, admin: AuthUser, slot_id: int, payload: dict[str, Any] | None
    ) -> ApiResult:
        session_id = _parse_session_id((payload or {}).get("session_id"))
        bound = self._bound_session(admin, slot_id, session_id, allow_inactive=True)
        if isinstance(bound, ApiResult):
            return bound
        ended = self._store.close_session(bound.session_id, now=self._clock()) or bound
        self._audit(bound.slot_id, "admin_live_access_closed", admin.user_id)
        logger.info("admin_live_access_closed slot=%s admin=%s", bound.slot_id, admin.user_id[:8])
        body = ended.to_public_dict(self._clock())
        body["status"] = ended.status if ended.status != STATUS_ACTIVE else "closed"
        body["active"] = False
        return ApiResult(200, body)

    def rental_snapshot(self, slot_id: int) -> dict[str, Any]:
        """Observational only. Used by tests to prove live access did not rent."""
        assigned = None
        if self._assignments is not None:
            getter = getattr(self._assignments, "get", None)
            if callable(getter):
                assigned = getter(int(slot_id))
        cleanup_required = False
        if self._cleanup is not None:
            required = getattr(self._cleanup, "is_required", None)
            if callable(required):
                cleanup_required = bool(required(int(slot_id)))
        return {
            "assigned": assigned is not None,
            "cleanup_required": cleanup_required,
        }

    def _bound_session(
        self,
        admin: AuthUser,
        slot_id: int,
        session_id: str | None,
        *,
        allow_inactive: bool = False,
    ) -> AdminLiveAccessSession | ApiResult:
        if not is_admin_user(admin):
            return ApiResult(403, error_body("admin_forbidden"))
        parsed = self._parse_slot(slot_id)
        if isinstance(parsed, ApiResult):
            return parsed
        sid = _parse_session_id(session_id)
        if sid is None:
            return ApiResult(400, error_body("invalid_request"))
        session = self._store.get(sid)
        if session is None:
            return ApiResult(404, error_body("live_access_not_found"))
        if session.admin_user_id != admin.user_id:
            return ApiResult(403, error_body("forbidden"))
        if session.slot_id != parsed:
            return ApiResult(403, error_body("live_access_slot_mismatch"))
        if allow_inactive:
            return session
        if not session.is_active(self._clock()):
            return ApiResult(403, error_body("session_expired"))
        return session

    def _parse_slot(self, slot_id: int) -> int | ApiResult:
        try:
            bay = int(slot_id)
        except (TypeError, ValueError):
            return ApiResult(400, error_body("invalid_slot"))
        if bay < MIN_SLOT or bay > MAX_SLOT:
            return ApiResult(400, error_body("invalid_slot"))
        if bay not in self._slots:
            return ApiResult(404, error_body("slot_not_found"))
        if self._gads_slots is not None and bay not in self._gads_slots:
            return ApiResult(503, error_body("phone_unavailable"))
        return bay

    def _require_ready(self, slot_id: int) -> tuple[str, Any] | ApiResult:
        if not self.enabled:
            return ApiResult(503, error_body("phone_unavailable"))
        if self._platform is None:
            return ApiResult(503, error_body("gads_unavailable"))
        device_id = self._slots.get(int(slot_id))
        if not device_id:
            return ApiResult(404, error_body("slot_not_found"))
        try:
            status = self._platform.device_status(slot_id=int(slot_id), device_id=device_id)
        except RemoteAccessPlatformError:
            logger.warning("admin_live_access_readiness_failed slot=%s", slot_id)
            return ApiResult(502, error_body("gads_unavailable"))
        if not status.registered:
            return ApiResult(503, error_body("phone_unavailable"))
        if not status.online:
            return ApiResult(
                409,
                error_body("phone_offline", message="The selected phone is offline"),
            )
        return device_id, self._platform

    def _forward_control(
        self,
        platform: Any,
        session: AdminLiveAccessSession,
        command: ControlCommand,
    ) -> bool | ApiResult:
        kwargs = {
            "device_id": session.device_id,
            "platform_username": GADS_HUB_ADMIN_USERNAME,
            "platform_password": "",
        }
        if command.action == "settings":
            # No dedicated GADS Settings endpoint. Admin opens Settings by tap.
            return True
        if command.action == "tap":
            tap = getattr(platform, "tap", None)
            forwarded = False
            if callable(tap):
                forwarded = bool(tap(**kwargs, x=int(command.x), y=int(command.y)))
            if not forwarded:
                return self._farm_input(session.slot_id, "tap", command)
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
                if command.duration_ms is not None:
                    try:
                        forwarded = bool(swipe(**swipe_kwargs, duration_ms=int(command.duration_ms)))
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

    def _farm_input(self, slot_id: int, kind: str, command: ControlCommand) -> bool | ApiResult:
        if self._farm is None:
            return False
        runner = getattr(self._farm, "run_task", None)
        if not callable(runner):
            return False
        payload: dict[str, Any] = {"kind": kind}
        if kind in {"tap", "swipe"}:
            payload.update({"x": command.x, "y": command.y})
        if kind == "swipe":
            payload.update({"x2": command.x2, "y2": command.y2})
            if command.duration_ms is not None:
                payload["duration_ms"] = int(command.duration_ms)
        try:
            response = runner(
                task_type="setup_session_input",
                farm_slot_id=int(slot_id),
                payload=payload,
                job_id=f"admin-live-{slot_id}",
            )
        except Exception:
            logger.warning("admin_live_access_farm_input_failed slot=%s", slot_id)
            return ApiResult(502, error_body("farm_unreachable"))
        if not getattr(response, "ok", False):
            if getattr(response, "error", None) == "action_not_supported":
                return ApiResult(501, error_body("action_not_supported"))
            return False
        return True

    def _audit(self, slot_id: int, event: str, admin_user_id: str) -> None:
        if self._record is None:
            return
        try:
            self._record(int(slot_id), event, f"admin={admin_user_id}")
        except Exception:
            logger.warning("admin_live_access_audit_failed slot=%s", slot_id)

    def _lock_for(self, slot_id: int) -> threading.Lock:
        with self._locks_guard:
            lock = self._slot_locks.get(int(slot_id))
            if lock is None:
                lock = threading.Lock()
                self._slot_locks[int(slot_id)] = lock
            return lock
