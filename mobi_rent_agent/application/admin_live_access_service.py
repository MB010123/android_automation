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
from uuid import UUID, uuid4

from application.admin_identity import is_admin_user
from application.device_airplane_mode import (
    airplane_is_on,
    public_airplane_body,
    requested_airplane_state,
)
from application.device_hardware_lockdown import request_device_access_mode
from application.device_screen_prepare import request_device_screen_prepare
from application.device_display_size import (
    DEVICE_DISPLAY_SIZE_TASK,
    attach_native_resolution,
    map_touch_to_native,
    parse_display_size_payload,
)
from application.in_app_control_policy import (
    NAV_ACTIONS,
    ControlCommand,
    decide_control,
)
from application.vps_api_contract import error_body
from domain.farm import MAX_FARM_SLOT_ID
from domain.remote_access import RemoteAccessPlatformError
from infrastructure.admin_live_access_store import (
    STATUS_ACTIVE,
    AdminLiveAccessSession,
    AdminLiveAccessStore,
)
from infrastructure.auth_models import AuthUser
from infrastructure.gads_remote_access import GADS_HUB_ADMIN_USERNAME
from infrastructure.slot_public_id import farm_slot_for_public_id

logger = logging.getLogger("vps_backend.admin_live_access")

MIN_SLOT = 1
MAX_SLOT = MAX_FARM_SLOT_ID
_MAX_TTL_MINUTES = 60
_MIN_TTL_MINUTES = 5
AIRPLANE_MODE_UNSUPPORTED_MESSAGE = (
    "Airplane Mode uses the Farm Agent `airplane_mode` task "
    "(settings put + AIRPLANE_MODE broadcast), not GADS."
)


@dataclass
class ApiResult:
    http_status: int
    body: dict[str, Any]


def _gads_error_category(exc: BaseException) -> str:
    text = str(exc).split(":", 1)[0].strip().replace(" ", "_")
    return text[:64] or "gads_error"


def _is_airplane_request(body: dict[str, Any]) -> bool:
    action = str(body.get("action") or "").strip().lower().replace("-", "_")
    if action in {"airplane_mode"}:
        return True
    return "enabled" in body and action in {"", "airplane_mode"}


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
        farm_status_fetcher: Callable[[], dict[str, Any]] | None = None,
        event_recorder: Callable[[int, str, str], None] | None = None,
        assignment_store: Any = None,
        cleanup_store: Any = None,
        session_ttl_minutes: int = 15,
        gads_slot_ids: tuple[int, ...] | None = None,
        workspace_map: dict[int, str] | None = None,
        slot_public_id_overrides: dict[str, int] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.enabled = bool(enabled)
        self._slots = {int(k): str(v) for k, v in (slot_device_map or {}).items()}
        self._workspaces = {
            int(k): str(v).strip()
            for k, v in (workspace_map or {}).items()
            if str(v).strip()
        }
        self._platform = platform
        self._store = store
        self._farm = farm_task_client
        self._farm_status = farm_status_fetcher
        self._record = event_recorder
        self._assignments = assignment_store
        self._cleanup = cleanup_store
        ttl = int(session_ttl_minutes)
        self._ttl_minutes = min(max(ttl, _MIN_TTL_MINUTES), _MAX_TTL_MINUTES)
        # Customer REMOTE_ACCESS_SLOT_IDS must not hide bays from an admin.
        self._gads_slots = (
            frozenset(int(s) for s in gads_slot_ids)
            if gads_slot_ids is not None
            else None
        )
        self._public_id_overrides = dict(slot_public_id_overrides or {})
        self._clock = clock
        self._locks_guard = threading.Lock()
        self._slot_locks: dict[int, threading.Lock] = {}
        self._native_resolution_cache: dict[int, tuple[int, int]] = {}

    def resolve_slot_reference(self, reference: Any) -> int | None:
        """Farm bay for a dashboard device reference.

        Accepts the bay number (``21``), the slot public UUID shown to
        Lovable/Supabase (``26876bff-...``), or the ADB serial
        (``RFCNB056WEV``). Returns None when nothing matches; bay-range and
        slot-map checks stay in ``_parse_slot``.
        """
        text = str(reference or "").strip()
        if not text:
            return None
        if text.isdigit():
            return int(text)
        try:
            canonical = str(UUID(text))
        except (TypeError, ValueError):
            canonical = None
        if canonical is not None:
            return farm_slot_for_public_id(canonical, self._public_id_overrides)
        needle = text.upper()
        for bay, serial in self._slots.items():
            if str(serial).strip().upper() == needle:
                return int(bay)
        return None

    def sweep_stale_sessions(self) -> int:
        return self._store.expire_stale(now=self._clock())

    def create_session(
        self, admin: AuthUser, slot_id: int, *, admin_mode: bool = True
    ) -> ApiResult:
        if not is_admin_user(admin):
            logger.info("admin_live_access_auth denied slot=%s", slot_id)
            return ApiResult(403, error_body("admin_forbidden"))
        parsed = self._parse_slot(slot_id)
        if isinstance(parsed, ApiResult):
            logger.info("admin_live_access_denied slot=%s reason=%s", slot_id, parsed.body.get("error"))
            return parsed
        logger.info(
            "admin_live_access_auth ok admin=%s slot=%s mapped=%s workspace=%s cleanup=%s rented=%s",
            admin.user_id[:8],
            parsed,
            parsed in self._slots,
            bool(self._workspaces.get(parsed)),
            self._cleanup_required(parsed),
            self._is_rented(parsed),
        )
        ready = self._require_ready(parsed)
        if isinstance(ready, ApiResult):
            logger.info("admin_live_access_denied slot=%s reason=%s", parsed, ready.body.get("error"))
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
                self._prepare_device_screen(parsed, wait_boot=False, admin_mode=admin_mode)
                return ApiResult(200, self._session_body(existing, parsed))
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
        self._prepare_device_screen(parsed, wait_boot=False, admin_mode=admin_mode)
        return ApiResult(201, self._session_body(session, parsed))

    def open_stream(
        self,
        admin: AuthUser,
        slot_id: int,
        session_id: str | None,
        *,
        admin_mode: bool = True,
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
        self._prepare_device_screen(bound.slot_id, wait_boot=False, admin_mode=admin_mode)
        opener = getattr(platform, "open_mjpeg_stream", None)
        if not callable(opener):
            return ApiResult(503, error_body("gads_unavailable"))
        try:
            stream = opener(
                device_id=bound.device_id,
                platform_username=GADS_HUB_ADMIN_USERNAME,
                platform_password="",
            )
        except RemoteAccessPlatformError as exc:
            logger.warning(
                "admin_live_access_stream_failed slot=%s category=%s",
                bound.slot_id,
                _gads_error_category(exc),
            )
            return ApiResult(502, error_body("gads_unavailable"))
        logger.info("admin_live_access_stream_open slot=%s first_frame=pending", bound.slot_id)
        return stream

    def control_session(
        self,
        admin: AuthUser,
        slot_id: int,
        payload: dict[str, Any] | None,
    ) -> ApiResult:
        raw = payload if isinstance(payload, dict) else {}
        from application.device_hardware_lockdown import parse_admin_flag

        admin_flag = parse_admin_flag(raw.get("admin")) if "admin" in raw else None
        control_body = {
            key: value for key, value in raw.items() if key not in {"session_id", "admin"}
        }
        if _is_airplane_request(control_body):
            if not is_admin_user(admin):
                return ApiResult(403, error_body("admin_forbidden"))
            parsed = self._parse_slot(slot_id)
            if isinstance(parsed, ApiResult):
                return parsed
            return self._run_airplane_mode(parsed, control_body)
        session_id = _parse_session_id(raw.get("session_id"))
        bound = self._bound_session(admin, slot_id, session_id)
        if isinstance(bound, ApiResult):
            return bound
        if admin_flag is not None:
            request_device_access_mode(self._farm, bound.slot_id, admin=admin_flag)
            if not control_body:
                return ApiResult(
                    200,
                    {
                        "ok": True,
                        "admin": bool(admin_flag),
                        "slot_id": bound.slot_id,
                        "session_id": bound.session_id,
                    },
                )
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
            return self._run_airplane_mode(bound.slot_id, control_body)
        ready = self._require_ready(bound.slot_id)
        if isinstance(ready, ApiResult):
            return ready
        device_id, platform = ready
        if device_id != bound.device_id:
            return ApiResult(409, error_body("phone_unavailable"))
        try:
            forwarded = self._forward_control(platform, bound, decision.command)
        except RemoteAccessPlatformError as exc:
            logger.warning(
                "admin_live_access_control_failed slot=%s category=%s",
                bound.slot_id,
                _gads_error_category(exc),
            )
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

    def run_airplane_mode(self, admin: AuthUser, slot_id: int, payload: dict[str, Any] | None) -> ApiResult:
        if not is_admin_user(admin):
            return ApiResult(403, error_body("admin_forbidden"))
        parsed = self._parse_slot(slot_id)
        if isinstance(parsed, ApiResult):
            return parsed
        return self._run_airplane_mode(parsed, payload if isinstance(payload, dict) else {})

    def _run_airplane_mode(self, slot_id: int, payload: dict[str, Any] | str | None) -> ApiResult:
        if self._farm is None:
            return ApiResult(503, error_body("farm_unreachable"))
        body = payload if isinstance(payload, dict) else {"state": payload}
        state = requested_airplane_state(body)
        try:
            response = self._farm.run_task(
                task_type="airplane_mode",
                farm_slot_id=int(slot_id),
                payload={"state": state, "enabled": state == "on"},
                job_id=str(uuid4()),
            )
        except Exception:
            return ApiResult(504, error_body("timeout"))
        if not getattr(response, "ok", False):
            status = int(getattr(response, "http_status", 0) or 502)
            if status in {0, 504}:
                return ApiResult(504, error_body("timeout"))
            return ApiResult(503, error_body("farm_unreachable"))
        details: dict[str, Any] = {}
        raw_body = getattr(response, "body", None)
        if isinstance(raw_body, dict):
            inner = raw_body.get("details")
            details = dict(inner) if isinstance(inner, dict) else dict(raw_body)
        applied = str(details.get("state") or state)
        public = public_airplane_body(
            slot_id=int(slot_id),
            state=applied,
            serial=str(details.get("serial") or self._slots.get(int(slot_id)) or "") or None,
        )
        public["enabled"] = airplane_is_on(applied)
        return ApiResult(200, public)

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
            return ApiResult(
                503,
                error_body(
                    "phone_unavailable",
                    message="This bay is not enabled for screen access",
                ),
            )
        return bay

    def _session_body(self, session: AdminLiveAccessSession, slot_id: int) -> dict[str, Any]:
        body = session.to_public_dict(self._clock())
        body["slot_id"] = int(slot_id)
        body["serial"] = self._slots.get(int(slot_id))
        if self._is_rented(slot_id) or self._cleanup_required(slot_id):
            body["customer_session_warning"] = (
                "This phone may have an active customer session or cleanup hold. "
                "Admin input can interrupt the customer. It does not end the rental "
                "or mark cleanup complete."
            )
        native, unavailable = self._native_resolution_for_slot(slot_id)
        attach_native_resolution(body, native, unavailable)
        return body

    def _prepare_device_screen(
        self, slot_id: int, *, wait_boot: bool, admin_mode: bool = True
    ) -> None:
        request_device_access_mode(self._farm, int(slot_id), admin=bool(admin_mode))
        request_device_screen_prepare(
            self._farm, int(slot_id), wait_boot=wait_boot, admin=bool(admin_mode)
        )
        self._native_resolution_cache.pop(int(slot_id), None)

    def _mapped_touch_command(self, slot_id: int, command: ControlCommand) -> ControlCommand:
        if command.action not in {"tap", "swipe"}:
            return command
        native, _unavailable = self._native_resolution_for_slot(slot_id)
        if native is None:
            return command
        start = map_touch_to_native(command.x, command.y, native[0], native[1])
        if start is None:
            return command
        if command.action == "tap":
            return ControlCommand(action="tap", x=start[0], y=start[1])
        end = map_touch_to_native(command.x2, command.y2, native[0], native[1])
        if end is None:
            return command
        return ControlCommand(
            action="swipe",
            x=start[0],
            y=start[1],
            x2=end[0],
            y2=end[1],
            duration_ms=command.duration_ms,
        )

    def _native_resolution_for_slot(self, slot_id: int) -> tuple[tuple[int, int] | None, str | None]:
        """Active input size: Farm ``wm size``, then GADS registered screen size.

        VPS has no ADB. Never uses the MJPEG frame size.
        """
        bay = int(slot_id)
        cached = self._native_resolution_cache.get(bay)
        if cached is not None:
            return cached, None
        size = self._farm_wm_size(bay)
        source = "wm_size"
        if size is None:
            size = self._gads_registered_size(bay)
            source = "gads_device"
        if size is None:
            logger.info(
                "admin_live_access_native_resolution slot=%s unavailable=wm_size_unavailable",
                bay,
            )
            return None, "wm_size_unavailable"
        self._native_resolution_cache[bay] = size
        logger.info(
            "admin_live_access_native_resolution slot=%s width=%s height=%s source=%s",
            bay,
            size[0],
            size[1],
            source,
        )
        return size, None

    def _farm_wm_size(self, bay: int) -> tuple[int, int] | None:
        if self._farm is None:
            return None
        try:
            response = self._farm.run_task(
                task_type=DEVICE_DISPLAY_SIZE_TASK,
                farm_slot_id=bay,
                payload={},
                job_id=f"admin-display-{bay}",
            )
        except Exception:
            logger.warning("admin_live_access_native_resolution_failed slot=%s", bay)
            return None
        body = getattr(response, "body", None)
        return parse_display_size_payload(body if isinstance(body, dict) else None)

    def _gads_registered_size(self, bay: int) -> tuple[int, int] | None:
        if self._platform is None:
            return None
        device_id = str(self._slots.get(int(bay)) or "").strip()
        if not device_id:
            return None
        getter = getattr(self._platform, "device_status", None)
        if not callable(getter):
            return None
        try:
            status = getter(
                slot_id=int(bay),
                device_id=device_id,
                workspace_id=self._workspace_for(bay),
                require_workspace=False,
            )
        except Exception:
            logger.warning("admin_live_access_gads_size_failed slot=%s", bay)
            return None
        try:
            width = int(getattr(status, "screen_width", None))
            height = int(getattr(status, "screen_height", None))
        except (TypeError, ValueError):
            return None
        if width <= 0 or height <= 0:
            return None
        return width, height

    def _workspace_for(self, slot_id: int) -> str | None:
        return self._workspaces.get(int(slot_id)) or None

    def _cleanup_required(self, slot_id: int) -> bool:
        if self._cleanup is None:
            return False
        required = getattr(self._cleanup, "is_required", None)
        return bool(required(int(slot_id))) if callable(required) else False

    def _is_rented(self, slot_id: int) -> bool:
        if self._assignments is None:
            return False
        getter = getattr(self._assignments, "get", None)
        return bool(callable(getter) and getter(int(slot_id)))

    def _require_ready(self, slot_id: int) -> tuple[str, Any] | ApiResult:
        if not self.enabled:
            return ApiResult(503, error_body("admin_live_access_not_configured"))
        if self._platform is None:
            return ApiResult(503, error_body("gads_unavailable"))
        device_id = self._slots.get(int(slot_id))
        if not device_id:
            return ApiResult(404, error_body("slot_not_found"))
        workspace = self._workspace_for(slot_id)
        try:
            status_fn = self._platform.device_status
            try:
                status = status_fn(
                    slot_id=int(slot_id),
                    device_id=device_id,
                    workspace_id=workspace,
                    require_workspace=False,
                )
            except TypeError:
                status = status_fn(
                    slot_id=int(slot_id),
                    device_id=device_id,
                    workspace_id=workspace,
                )
        except RemoteAccessPlatformError as exc:
            logger.warning(
                "admin_live_access_readiness_failed slot=%s category=%s",
                slot_id,
                _gads_error_category(exc),
            )
            if self._adb_online(int(slot_id)) is True:
                logger.info(
                    "admin_live_access_ready slot=%s gads_error adb_online=true",
                    slot_id,
                )
                return device_id, self._platform
            return ApiResult(502, error_body("gads_unavailable"))
        logger.info(
            "admin_live_access_ready slot=%s registered=%s online=%s workspace_mapped=%s",
            slot_id,
            bool(status.registered),
            bool(status.online),
            bool(workspace),
        )
        if status.registered and status.online:
            return device_id, self._platform
        if self._adb_online(int(slot_id)) is True:
            logger.info(
                "admin_live_access_ready slot=%s registered=%s gads_online=%s adb_online=true",
                slot_id,
                bool(status.registered),
                bool(status.online),
            )
            return device_id, self._platform
        if not status.registered:
            return ApiResult(
                503,
                error_body(
                    "phone_unavailable",
                    message="The selected phone is not registered with the screen service",
                ),
            )
        return ApiResult(
            409,
            error_body("phone_offline", message="The selected phone is offline"),
        )

    def _adb_online(self, slot_id: int) -> bool | None:
        if self._farm_status is None:
            return None
        try:
            farm = self._farm_status()
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(farm, dict) or "offline_slots" not in farm:
            return None
        try:
            offline = {int(item) for item in (farm.get("offline_slots") or [])}
        except (TypeError, ValueError):
            return None
        return int(slot_id) not in offline

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
            return self._farm_input(session.slot_id, "settings", command)
        if command.action == "tap":
            mapped = self._mapped_touch_command(session.slot_id, command)
            tap = getattr(platform, "tap", None)
            forwarded = False
            if callable(tap):
                forwarded = bool(tap(**kwargs, x=int(mapped.x), y=int(mapped.y)))
            if not forwarded:
                return self._farm_input(session.slot_id, "tap", mapped)
            return True
        if command.action == "type":
            type_text = getattr(platform, "type_text", None)
            if not callable(type_text):
                return False
            type_text(**kwargs, text=str(command.text))
            return True
        if command.action == "swipe":
            mapped = self._mapped_touch_command(session.slot_id, command)
            swipe = getattr(platform, "swipe", None)
            forwarded = False
            if callable(swipe):
                swipe_kwargs = {
                    **kwargs,
                    "x": int(mapped.x),
                    "y": int(mapped.y),
                    "x2": int(mapped.x2),
                    "y2": int(mapped.y2),
                }
                if command.duration_ms is not None:
                    try:
                        forwarded = bool(swipe(**swipe_kwargs, duration_ms=int(command.duration_ms)))
                    except TypeError:
                        forwarded = bool(swipe(**swipe_kwargs))
                else:
                    forwarded = bool(swipe(**swipe_kwargs))
            if not forwarded:
                return self._farm_input(session.slot_id, "swipe", mapped)
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
        payload: dict[str, Any] = {"kind": kind, "admin": True}
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
