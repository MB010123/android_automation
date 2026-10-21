"""Admin live access by farm bay: no rental, no cleanup side effects."""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.admin_identity import resolve_admin_role
from application.admin_live_access_service import AdminLiveAccessService
from application.auth_service import AuthService
from application.vps_farm_management_service import VpsFarmManagementService
from application.vps_job_worker import VpsJobWorker
from application.vps_lovable_routes import parse_route
from infrastructure.admin_live_access_store import AdminLiveAccessStore
from infrastructure.auth_models import AuthUser
from infrastructure.auth_rate_limiter import AuthRateLimiter
from infrastructure.device_cleanup_store import DeviceCleanupStore
from infrastructure.gads_remote_access import GADS_HUB_ADMIN_USERNAME
from infrastructure.slot_assignment_store import SlotAssignmentStore
from infrastructure.slot_event_store import SlotEventStore
from infrastructure.slot_reservation_store import SlotReservationStore
from infrastructure.slot_status_store import SlotStatusStore
from infrastructure.supabase_jwt import sign_supabase_access_token, verify_supabase_access_token
from infrastructure.vps_job_store import VpsJobStore
from infrastructure.vps_rate_limiter import VpsRateLimiter
from tests.fakes_supabase import MemoryGoTrue, MemoryTenant, seed_owned_slot
from tests.test_remote_access_poc import (
    CUSTOMER_A,
    FARM_TOKEN,
    SLOT1_SERIAL,
    SLOT2_SERIAL,
    STRONG,
    FakeClock,
    FakeFarm,
    FakePlatform,
    _rental,
    _service,
)

ADMIN_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
USER_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
JWT_SECRET = "unit-test-supabase-jwt-secret-not-for-production"


def _admin() -> AuthUser:
    return AuthUser(user_id=ADMIN_ID, email="admin@example.com", role="admin")


def _user() -> AuthUser:
    return AuthUser(user_id=USER_ID, email="user@example.com", role="user")


def _admin_service(
    tmp_path: Path,
    *,
    platform: FakePlatform | None = None,
    clock: FakeClock | None = None,
    enabled: bool = True,
    ttl_minutes: int = 15,
    cleanup_store: DeviceCleanupStore | None = None,
    assignment_store: SlotAssignmentStore | None = None,
    farm: FakeFarm | None = None,
    gads_slot_ids: tuple[int, ...] | None = (1, 2),
    slot_device_map: dict[int, str] | None = None,
    workspace_map: dict[int, str] | None = None,
) -> tuple[AdminLiveAccessService, FakePlatform, FakeClock, AdminLiveAccessStore]:
    clock = clock or FakeClock()
    platform = platform or FakePlatform(registered={SLOT1_SERIAL, SLOT2_SERIAL})
    store = AdminLiveAccessStore(tmp_path / f"admin-live-{uuid.uuid4().hex}.sqlite")
    service = AdminLiveAccessService(
        enabled=enabled,
        slot_device_map=slot_device_map or {1: SLOT1_SERIAL, 2: SLOT2_SERIAL},
        platform=platform,
        store=store,
        farm_task_client=farm,
        assignment_store=assignment_store,
        cleanup_store=cleanup_store,
        session_ttl_minutes=ttl_minutes,
        gads_slot_ids=gads_slot_ids,
        workspace_map=workspace_map,
        clock=clock,
    )
    return service, platform, clock, store


def test_admin_identity_allowlist_and_app_metadata_only():
    uid = "11111111-1111-4111-8111-111111111111"
    assert resolve_admin_role(user_id=uid, allowlist=frozenset({uid})) == "admin"
    assert (
        resolve_admin_role(
            user_id=uid,
            claims={"app_metadata": {"mobi_rent_role": "admin"}},
        )
        == "admin"
    )
    assert (
        resolve_admin_role(user_id=uid, claims={"app_metadata": {"role": "admin"}})
        == "admin"
    )
    assert (
        resolve_admin_role(user_id=uid, claims={"user_metadata": {"role": "admin"}})
        == "user"
    )
    assert resolve_admin_role(user_id=uid, claims={"role": "authenticated"}) == "user"
    assert resolve_admin_role(user_id=uid) == "user"


def test_auth_service_sets_admin_from_allowlist_and_app_metadata_not_user_metadata():
    gotrue = MemoryGoTrue()
    allowlisted = AuthService(
        supabase=gotrue,
        jwt_secret=JWT_SECRET,
        admin_user_ids=frozenset({ADMIN_ID}),
    )
    token = sign_supabase_access_token(
        user_id=ADMIN_ID,
        email="admin@example.com",
        secret=JWT_SECRET,
        issuer=gotrue.project_url,
    )
    ctx = allowlisted.validate_user_token(token)
    assert ctx is not None and ctx.user is not None
    assert ctx.user.role == "admin"

    meta = AuthService(supabase=gotrue, jwt_secret=JWT_SECRET)
    admin_token = sign_supabase_access_token(
        user_id=USER_ID,
        email="ops@example.com",
        secret=JWT_SECRET,
        issuer=gotrue.project_url,
        app_metadata={"role": "admin"},
    )
    assert meta.validate_user_token(admin_token).user.role == "admin"
    spoof = sign_supabase_access_token(
        user_id=USER_ID,
        email="ops@example.com",
        secret=JWT_SECRET,
        issuer=gotrue.project_url,
        user_metadata={"role": "admin"},
    )
    assert meta.validate_user_token(spoof).user.role == "user"


def test_auth_rejects_expired_malformed_and_forged_jwt_roles():
    gotrue = MemoryGoTrue()
    svc = AuthService(
        supabase=gotrue,
        jwt_secret=JWT_SECRET,
        admin_user_ids=frozenset({ADMIN_ID}),
    )
    expired = sign_supabase_access_token(
        user_id=ADMIN_ID,
        email="admin@example.com",
        secret=JWT_SECRET,
        issuer=gotrue.project_url,
        ttl_seconds=1,
        now=1_000_000.0,
    )
    assert (
        verify_supabase_access_token(
            expired, JWT_SECRET, issuer=gotrue.project_url, now=1_000_010.0
        )
        is None
    )
    assert svc.validate_user_token(expired) is None
    assert svc.validate_user_token("not-a-jwt") is None
    assert svc.validate_user_token("a.b.c") is None
    wrong = sign_supabase_access_token(
        user_id=ADMIN_ID,
        email="admin@example.com",
        secret="other-secret-not-the-vps-secret",
        issuer=gotrue.project_url,
    )
    assert svc.validate_user_token(wrong) is None
    empty = AuthService(supabase=gotrue, jwt_secret=JWT_SECRET)
    customer = sign_supabase_access_token(
        user_id=USER_ID,
        email="cust@example.com",
        secret=JWT_SECRET,
        issuer=gotrue.project_url,
    )
    ctx = empty.validate_user_token(customer)
    assert ctx is not None and ctx.user.role == "user"


def test_get_user_role_admin_is_stripped_without_allowlist_or_app_metadata():
    gotrue = MemoryGoTrue()
    session = gotrue.signup("spoof@example.com", STRONG)
    session.user.role = "admin"
    svc = AuthService(supabase=gotrue, jwt_secret="")
    ctx = svc.validate_user_token(session.access_token)
    assert ctx is not None and ctx.user.role == "user"


def test_disabled_service_and_missing_platform_fail_closed(tmp_path: Path):
    off, _, _, _ = _admin_service(tmp_path, enabled=False)
    disabled = off.create_session(_admin(), 1)
    assert disabled.http_status == 503
    assert disabled.body["error"] == "admin_live_access_not_configured"
    none, _, _, _ = _admin_service(tmp_path)
    none._platform = None
    assert none.create_session(_admin(), 1).body["error"] == "gads_unavailable"


def test_poc_flag_does_not_build_admin_live_access(tmp_path: Path):
    from types import SimpleNamespace

    mod = _load_mod()
    svc, store = mod._build_admin_live_access_service(
        SimpleNamespace(admin_live_access_enabled=False, remote_access_poc_enabled=True),
        farm_task_client=None,
        event_store=None,
        assignment_store=None,
        cleanup_store=None,
        db_path=tmp_path / "unused.sqlite",
    )
    assert svc is None and store is None


def test_admin_ids_enable_builder_without_explicit_flag(tmp_path: Path):
    from types import SimpleNamespace

    mod = _load_mod()
    platform = FakePlatform(registered={SLOT1_SERIAL})
    svc, store = mod._build_admin_live_access_service(
        SimpleNamespace(
            admin_live_access_enabled=False,
            vps_admin_user_ids=(ADMIN_ID,),
            slot_map_path=str(tmp_path / "missing-slot-map.json"),
            remote_access_workspace_map_path=None,
            remote_access_workspace_id=None,
            admin_live_access_ttl_minutes=15,
        ),
        farm_task_client=None,
        event_store=None,
        assignment_store=None,
        cleanup_store=None,
        db_path=tmp_path / "admin-live-builder.sqlite",
        remote_platform=platform,
    )
    assert svc is not None and store is not None
    assert svc.enabled is True
    store.close()


def test_admin_can_open_available_cleanup_and_rented_phones_without_rental_side_effects(
    tmp_path: Path,
):
    tenant = MemoryTenant()
    platform = FakePlatform(registered={SLOT1_SERIAL, SLOT2_SERIAL})
    cleanup = DeviceCleanupStore(tmp_path / "cleanup.sqlite")
    assign = SlotAssignmentStore(tmp_path / "assign.sqlite")
    admin_svc, platform, _, _ = _admin_service(
        tmp_path, platform=platform, cleanup_store=cleanup, assignment_store=assign
    )
    customer_svc, ra_store, _ = _service(tmp_path, tenant=tenant, platform=platform)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    created = customer_svc.create_remote_access(CUSTOMER_A, None, rental)
    assert created.http_status == 201
    grants_before = [c for c in platform.calls if c[0] == "grant"]

    available = admin_svc.create_session(_admin(), 2)
    assert available.http_status == 201
    assert available.body["slot_id"] == 2
    assert available.body["rental_unchanged"] is True
    assert "platform_login" not in available.body
    assert SLOT2_SERIAL not in json.dumps(available.body)

    cleanup.mark_required(2, rental, detail="awaiting_operator")
    cleanup_view = admin_svc.create_session(_admin(), 2)
    assert cleanup_view.http_status == 200
    assert cleanup.is_required(2) is True
    assert "customer_session_warning" in cleanup_view.body

    rented = admin_svc.create_session(_admin(), 1)
    assert rented.http_status == 201
    assert rented.body["slot_id"] == 1
    grants_after = [c for c in platform.calls if c[0] == "grant"]
    assert grants_after == grants_before
    assert ra_store.get(rental) is not None
    assert ra_store.get(rental).status == "active"
    assert tenant.owner_of_slot(1) == CUSTOMER_A
    assert any(c[0] == "status" for c in platform.calls)
    assert not any(c[0] == "release" for c in platform.calls)

    closed = admin_svc.close_session(
        _admin(), 1, {"session_id": rented.body["session_id"]}
    )
    assert closed.http_status == 200
    assert closed.body["active"] is False
    assert ra_store.get(rental).status == "active"
    assert cleanup.is_required(2) is True
    assert tenant.owner_of_slot(1) == CUSTOMER_A


def test_non_admin_invalid_slot_and_cross_slot_control_rejected(tmp_path: Path):
    svc, platform, _, _ = _admin_service(tmp_path)
    denied = svc.create_session(_user(), 1)
    assert denied.http_status == 403
    assert denied.body["error"] == "admin_forbidden"
    assert not any(c[0] == "grant" for c in platform.calls)

    assert svc.create_session(_admin(), 0).body["error"] == "invalid_slot"
    assert svc.create_session(_admin(), 33).body["error"] == "invalid_slot"
    assert svc.create_session(_admin(), 9).body["error"] == "slot_not_found"
    assert svc.create_session(_admin(), 21).body["error"] == "slot_not_found"

    opened = svc.create_session(_admin(), 1)
    sid = opened.body["session_id"]
    cross = svc.control_session(
        _admin(), 2, {"session_id": sid, "action": "home"}
    )
    assert cross.http_status == 403
    assert cross.body["error"] == "live_access_slot_mismatch"
    assert not any(c[0] == "home" for c in platform.calls)


def test_stream_auth_expiry_and_supported_controls(tmp_path: Path):
    clock = FakeClock()
    svc, platform, clock, _ = _admin_service(tmp_path, clock=clock, ttl_minutes=5)
    created = svc.create_session(_admin(), 1)
    sid = created.body["session_id"]

    missing = svc.open_stream(_admin(), 1, None)
    assert missing.http_status == 400
    unknown = svc.open_stream(_admin(), 1, str(uuid.uuid4()))
    assert unknown.http_status == 404

    stream = svc.open_stream(_admin(), 1, sid)
    assert getattr(stream, "status_code", None) == 200
    assert platform.calls[-1][0] == "stream"
    assert platform.calls[-1][1]["username"] == GADS_HUB_ADMIN_USERNAME

    tap = svc.control_session(
        _admin(), 1, {"session_id": sid, "action": "tap", "x": 10, "y": 20}
    )
    assert tap.http_status == 200 and tap.body["forwarded"] is True
    home = svc.control_session(_admin(), 1, {"session_id": sid, "action": "home"})
    assert home.http_status == 200
    shade = svc.control_session(
        _admin(), 1, {"session_id": sid, "action": "notification_shade"}
    )
    assert shade.http_status == 200
    qs = svc.control_session(
        _admin(), 1, {"session_id": sid, "action": "quick_settings"}
    )
    assert qs.http_status == 200
    recents = svc.control_session(_admin(), 1, {"session_id": sid, "action": "recents"})
    assert recents.http_status == 200
    typed = svc.control_session(
        _admin(), 1, {"session_id": sid, "action": "type", "text": "hello"}
    )
    assert typed.http_status == 200
    swipe = svc.control_session(
        _admin(),
        1,
        {"session_id": sid, "action": "swipe", "x": 10, "y": 20, "x2": 80, "y2": 20},
    )
    assert swipe.http_status == 200
    rotate = svc.control_session(
        _admin(), 1, {"session_id": sid, "action": "rotate", "orientation": "portrait"}
    )
    assert rotate.http_status == 200
    settings = svc.control_session(
        _admin(), 1, {"session_id": sid, "action": "settings"}
    )
    assert settings.http_status == 200
    airplane = svc.control_session(
        _admin(), 1, {"session_id": sid, "action": "airplane_mode", "state": "on"}
    )
    assert airplane.http_status == 501
    assert airplane.body["error"] == "action_not_supported"
    bad = svc.control_session(
        _admin(), 1, {"session_id": sid, "action": "adb", "command": "shell"}
    )
    assert bad.http_status == 403
    identity = svc.control_session(
        _admin(),
        1,
        {"session_id": sid, "action": "tap", "x": 1, "y": 1, "udid": SLOT1_SERIAL},
    )
    assert identity.http_status == 403
    huge = svc.control_session(
        _admin(), 1, {"session_id": sid, "action": "tap", "x": 99999, "y": 1}
    )
    assert huge.http_status == 422

    clock.now += 6 * 60
    expired = svc.open_stream(_admin(), 1, sid)
    assert expired.http_status == 403
    assert expired.body["error"] == "session_expired"
    expired_ctl = svc.control_session(
        _admin(), 1, {"session_id": sid, "action": "home"}
    )
    assert expired_ctl.http_status == 403


def test_live_access_cannot_mark_cleanup_and_pending_stays_unavailable(tmp_path: Path):
    cleanup = DeviceCleanupStore(tmp_path / "cleanup.sqlite")
    reservations = SlotReservationStore(tmp_path / "res.sqlite")
    jobs = VpsJobStore(tmp_path / "jobs.sqlite")
    assign = SlotAssignmentStore(tmp_path / "assign.sqlite")
    events = SlotEventStore(tmp_path / "events.sqlite")
    tenant = MemoryTenant()

    class SyncWorker(VpsJobWorker):
        def enqueue_process(self, job_id: str) -> None:
            return None

    farm_svc = VpsFarmManagementService(
        job_store=jobs,
        assignment_store=assign,
        reservation_store=reservations,
        event_store=events,
        job_worker=SyncWorker(
            job_store=jobs,
            assignment_store=assign,
            event_store=events,
            farm_task_client=None,
            poll_interval_seconds=3600.0,
            auth_store=tenant,
        ),
        farm_status_fetcher=lambda: {
            "ok": True,
            "offline_slots": [],
            "mapped_slots": [1, 2],
            "slot_count": 2,
            "adb_online": 2,
        },
        known_farm_slots={1, 2},
        rate_limiter=VpsRateLimiter(per_slot_limit=100, global_limit=1000),
        status_store=SlotStatusStore(tmp_path / "status.sqlite"),
        auth_store=tenant,
        cleanup_store=cleanup,
    )
    rental = str(uuid.uuid4())
    cleanup.mark_required(1, rental, detail="awaiting_operator")
    assert 1 not in {row["bay"] for row in farm_svc.list_available_slots().body["available"]}

    svc, _, _, _ = _admin_service(tmp_path, cleanup_store=cleanup)
    opened = svc.create_session(_admin(), 1)
    assert opened.http_status == 201
    closed = svc.close_session(
        _admin(), 1, {"session_id": opened.body["session_id"]}
    )
    assert closed.http_status == 200
    assert cleanup.is_required(1) is True
    assert 1 not in {row["bay"] for row in farm_svc.list_available_slots().body["available"]}
    assert "cleanup" not in json.dumps(closed.body).lower() or closed.body["cleanup_unchanged"] is True

    second = str(uuid.uuid4())
    tenant.ensure_profile(USER_ID, "b@example.com")
    seed_owned_slot(tenant, bay=1, rental_id=second, user_id=USER_ID)
    blocked = farm_svc.reserve_slot(1, {"rental_id": second, "user_id": USER_ID})
    assert blocked.http_status == 409
    assert blocked.body["error"] == "cleanup_required"


def test_offline_and_gads_failures_are_safe(tmp_path: Path):
    platform = FakePlatform(registered={SLOT1_SERIAL, SLOT2_SERIAL})
    platform.online[SLOT1_SERIAL] = False
    svc, _, _, _ = _admin_service(tmp_path, platform=platform)
    offline = svc.create_session(_admin(), 1)
    assert offline.http_status == 409
    assert offline.body["error"] == "phone_offline"
    assert "SERIAL" not in json.dumps(offline.body)

    platform.online[SLOT1_SERIAL] = True
    platform.status_error = "gads_unreachable: Timeout"
    failed = svc.create_session(_admin(), 1)
    assert failed.http_status == 502
    assert failed.body["error"] == "gads_unavailable"
    assert "gads_unreachable" not in json.dumps(failed.body)

    platform.status_error = None
    platform.registered.discard(SLOT1_SERIAL)
    missing = svc.create_session(_admin(), 1)
    assert missing.http_status == 503
    assert missing.body["error"] == "phone_unavailable"


def test_admin_opens_bay_outside_customer_workspace_and_slot_filter(tmp_path: Path):
    serial20 = "SERIAL-SLOT20"
    platform = FakePlatform(registered={serial20})
    platform.device_workspace = {serial20: "ws-bay-20"}
    svc, platform, _, _ = _admin_service(
        tmp_path,
        platform=platform,
        gads_slot_ids=None,
        slot_device_map={20: serial20},
        workspace_map={20: "ws-bay-20"},
    )
    created = svc.create_session(_admin(), 20)
    assert created.http_status == 201
    status_calls = [c for c in platform.calls if c[0] == "status"]
    assert status_calls[-1][1]["require_workspace"] is False
    assert status_calls[-1][1]["workspace_id"] == "ws-bay-20"
    assert serial20 not in json.dumps(created.body)


def test_admin_live_access_returns_farm_wm_size_not_stream_frame(tmp_path: Path):
    farm = FakeFarm()
    farm.display_width = 1344
    farm.display_height = 2992
    svc, _, _, _ = _admin_service(tmp_path, farm=farm)
    created = svc.create_session(_admin(), 1)
    assert created.http_status == 201
    assert created.body["coordinate_space"] == "native_device_pixels"
    assert created.body["native_resolution"] == {"width": 1344, "height": 2992}
    assert created.body["screen_width"] == 1344
    assert created.body["screen_height"] == 2992
    assert created.body["native_resolution"] != {"width": 720, "height": 1480}
    assert created.body["native_resolution"] != {"width": 720, "height": 1600}
    assert "native_resolution_unavailable" not in created.body
    sized = [t for t in farm.tasks if t["type"] == "device_display_size"]
    assert len(sized) == 1
    assert sized[0]["slot"] == 1
    reused = svc.create_session(_admin(), 1)
    assert reused.http_status == 200
    assert reused.body["native_resolution"] == {"width": 1344, "height": 2992}
    assert len([t for t in farm.tasks if t["type"] == "device_display_size"]) == 1


def test_admin_live_access_uses_gads_screen_when_farm_wm_size_missing(tmp_path: Path):
    farm = FakeFarm()
    farm.fail_types.add("device_display_size")
    platform = FakePlatform(registered={SLOT1_SERIAL, SLOT2_SERIAL})
    platform.screen_size[SLOT1_SERIAL] = (1080, 2400)
    svc, _, _, _ = _admin_service(tmp_path, farm=farm, platform=platform)
    created = svc.create_session(_admin(), 1)
    assert created.http_status == 201
    assert created.body["native_resolution"] == {"width": 1080, "height": 2400}
    assert created.body["screen_width"] == 1080
    assert created.body["screen_height"] == 2400
    assert "native_resolution_unavailable" not in created.body


def test_admin_live_access_omits_invented_resolution_when_wm_size_missing(tmp_path: Path):
    farm = FakeFarm()
    farm.fail_types.add("device_display_size")
    svc, _, _, _ = _admin_service(tmp_path, farm=farm)
    created = svc.create_session(_admin(), 1)
    assert created.http_status == 201
    assert created.body["coordinate_space"] == "native_device_pixels"
    assert "native_resolution" not in created.body
    assert created.body["native_resolution_unavailable"] == "wm_size_unavailable"


def test_admin_live_access_reports_unavailable_resolution_without_farm(tmp_path: Path):
    svc, _, _, _ = _admin_service(tmp_path)
    created = svc.create_session(_admin(), 1)
    assert created.http_status == 201
    assert created.body["coordinate_space"] == "native_device_pixels"
    assert "native_resolution" not in created.body
    assert created.body["native_resolution_unavailable"] == "wm_size_unavailable"


def test_admin_does_not_disable_customer_session_restrictions(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform(registered={SLOT1_SERIAL})
    admin_svc, platform, _, _ = _admin_service(tmp_path, platform=platform)
    customer_svc, _, _ = _service(tmp_path, tenant=tenant, platform=platform)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    created = customer_svc.create_remote_access(CUSTOMER_A, None, rental)
    assert created.http_status == 201
    opened = admin_svc.create_session(_admin(), 1)
    qs = admin_svc.control_session(
        _admin(),
        1,
        {"session_id": opened.body["session_id"], "action": "quick_settings"},
    )
    assert qs.http_status == 200
    shade = customer_svc.control_session(
        CUSTOMER_A, rental, {"action": "notification_shade"}
    )
    assert shade.http_status == 200
    assert shade.body["restriction"] == "shade_blocked"
    assert "notification_shade" not in {c[0] for c in platform.calls}


def test_other_admin_cannot_use_session(tmp_path: Path):
    svc, _, _, _ = _admin_service(tmp_path)
    opened = svc.create_session(_admin(), 1)
    other = AuthUser(user_id=str(uuid.uuid4()), email="other@example.com", role="admin")
    stolen = svc.control_session(
        other, 1, {"session_id": opened.body["session_id"], "action": "home"}
    )
    assert stolen.http_status == 403
    assert stolen.body["error"] == "forbidden"


def _load_mod():
    path = ROOT / "tools" / "vps_backend_server.py"
    spec = importlib.util.spec_from_file_location("vps_backend_server_admin_live", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def _http(method: str, url: str, *, token: str | None = None, body: dict | None = None):
    data = None
    headers: dict[str, str] = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            raw = resp.read()
            if "multipart/x-mixed-replace" in (resp.headers.get("Content-Type") or ""):
                return resp.status, raw, dict(resp.headers)
            return resp.status, json.loads(raw.decode()), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        return exc.code, (json.loads(raw) if raw else {}), dict(exc.headers)


def _start_admin_http(tmp_path: Path):
    from application.vps_farm_management_service import VpsFarmManagementService
    from application.vps_job_worker import VpsJobWorker

    mod = _load_mod()
    Handler = mod.Handler
    gotrue = MemoryGoTrue()
    tenant = MemoryTenant()
    admin_session = gotrue.signup("admin@example.com", STRONG)
    assert not isinstance(admin_session, Exception)
    admin_id = admin_session.user.user_id
    platform = FakePlatform(registered={SLOT1_SERIAL, SLOT2_SERIAL})
    cleanup = DeviceCleanupStore(tmp_path / "cleanup.sqlite")

    class SyncWorker(VpsJobWorker):
        def enqueue_process(self, job_id: str) -> None:
            return None

    jobs = VpsJobStore(tmp_path / "jobs.sqlite")
    assign = SlotAssignmentStore(tmp_path / "assign.sqlite")
    events = SlotEventStore(tmp_path / "events.sqlite")
    status = SlotStatusStore(tmp_path / "status.sqlite")
    worker = SyncWorker(
        job_store=jobs,
        assignment_store=assign,
        event_store=events,
        farm_task_client=None,
        poll_interval_seconds=3600.0,
        auth_store=tenant,
    )
    farm_svc = VpsFarmManagementService(
        job_store=jobs,
        assignment_store=assign,
        event_store=events,
        job_worker=worker,
        farm_status_fetcher=lambda: {
            "ok": True,
            "offline_slots": [],
            "mapped_slots": [1, 2],
            "slot_count": 2,
            "adb_online": 2,
        },
        known_farm_slots={1, 2},
        rate_limiter=VpsRateLimiter(per_slot_limit=100, global_limit=1000),
        status_store=status,
        auth_store=tenant,
        cleanup_store=cleanup,
    )
    admin_store = AdminLiveAccessStore(tmp_path / "admin-live.sqlite")
    farm = FakeFarm()
    admin_svc = AdminLiveAccessService(
        enabled=True,
        slot_device_map={1: SLOT1_SERIAL, 2: SLOT2_SERIAL},
        platform=platform,
        store=admin_store,
        farm_task_client=farm,
        cleanup_store=cleanup,
        assignment_store=assign,
        gads_slot_ids=(1, 2),
    )
    customer_svc, _, _ = _service(tmp_path, tenant=tenant, platform=platform)
    farm_svc.set_remote_access(customer_svc)
    Handler.farm_service_token = FARM_TOKEN
    Handler.auth_service = AuthService(
        supabase=gotrue, tenant=tenant, admin_user_ids=frozenset({admin_id})
    )
    Handler.auth_rate_limiter = AuthRateLimiter(limit=100, window_seconds=60.0, max_keys=64)
    Handler.farm_management_service = farm_svc
    Handler.slot_sms_service = None
    Handler.allowed_origins = frozenset()
    Handler.auth_cookie_name = None
    Handler.webhook_secret = None
    Handler.tenant_store = tenant
    Handler.remote_access_service = customer_svc
    Handler.admin_live_access_service = admin_svc
    Handler.admin_live_access_rate_limiter = VpsRateLimiter(
        per_slot_limit=100, global_limit=1000
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1], tenant, platform, cleanup, admin_id


def test_http_admin_live_access_auth_stream_and_no_rental(tmp_path: Path):
    server, port, tenant, platform, cleanup, _admin_id = _start_admin_http(tmp_path)
    base = f"http://127.0.0.1:{port}"
    try:
        status, body, _ = _http("POST", f"{base}/auth/login", body={"email": "admin@example.com", "password": STRONG})
        assert status == 200, body
        admin_token = body["session"]["access_token"]
        status, me, _ = _http("GET", f"{base}/auth/me", token=admin_token)
        assert status == 200 and me["user"]["role"] == "admin"

        status, created, _ = _http("POST", f"{base}/auth/signup", body={"email": "cust@example.com", "password": STRONG})
        assert status == 200
        user_token = created["session"]["access_token"]
        rental = _rental(tenant, bay=1, user_id=created["user"]["id"])
        status, remote, _ = _http(
            "POST", f"{base}/rentals/{rental}/remote-access", token=user_token, body={}
        )
        assert status == 201, remote

        status, denied, _ = _http("POST", f"{base}/admin/devices/1/live-access", token=user_token, body={"role": "admin"})
        assert status == 403 and denied["error"] == "admin_forbidden"
        status, _, _ = _http("POST", f"{base}/admin/devices/1/live-access", body={})
        assert status == 401
        status, farm_denied, _ = _http(
            "POST", f"{base}/admin/devices/1/live-access", token=FARM_TOKEN, body={}
        )
        assert status == 401

        cleanup.mark_required(2, rental, detail="awaiting_operator")
        status, live, _ = _http("POST", f"{base}/admin/devices/2/live-access", token=admin_token, body={"role": "user"})
        assert status == 201, live
        assert live["slot_id"] == 2
        assert live["rental_unchanged"] is True
        assert live["coordinate_space"] == "native_device_pixels"
        assert live["native_resolution"] == {"width": 1440, "height": 3120}
        sid = live["session_id"]

        status, rented_live, _ = _http("POST", f"{base}/admin/devices/1/live-access", token=admin_token, body={})
        assert status == 201, rented_live
        status, still_remote, _ = _http("GET", f"{base}/rentals/{rental}/remote-access", token=user_token)
        assert status == 200 and still_remote["active"] is True
        assert cleanup.is_required(2) is True

        status, frames, headers = _http(
            "GET", f"{base}/admin/devices/2/live-access/stream?session={sid}", token=admin_token
        )
        assert status == 200
        assert b"--frame" in frames
        assert "gads" not in json.dumps(dict(headers)).lower()

        status, ctl, _ = _http(
            "POST",
            f"{base}/admin/devices/2/live-access/control",
            token=admin_token,
            body={"session_id": sid, "action": "tap", "x": 12, "y": 40},
        )
        assert status == 200 and ctl["forwarded"] is True
        status, cross, _ = _http(
            "POST",
            f"{base}/admin/devices/1/live-access/control",
            token=admin_token,
            body={"session_id": sid, "action": "home"},
        )
        assert status == 403 and cross["error"] == "live_access_slot_mismatch"

        status, closed, _ = _http(
            "POST",
            f"{base}/admin/devices/2/live-access/close",
            token=admin_token,
            body={"session_id": sid},
        )
        assert status == 200 and closed["active"] is False
        assert cleanup.is_required(2) is True
        status, after_remote, _ = _http("GET", f"{base}/rentals/{rental}/remote-access", token=user_token)
        assert status == 200 and after_remote["active"] is True
        assert tenant.owner_of_slot(1) == created["user"]["id"]
        assert not any(c[0] == "release" for c in platform.calls)

        sid1 = rented_live["session_id"]
        for path, method, body in (
            (f"/admin/devices/1/live-access/stream?session={sid1}", "GET", None),
            (f"/admin/devices/1/live-access/control", "POST", {"session_id": sid1, "action": "home"}),
            (f"/admin/devices/1/live-access/close", "POST", {"session_id": sid1}),
        ):
            status, denied, _ = _http(method, f"{base}{path}", body=body)
            assert status == 401, path
            status, farm_denied, _ = _http(method, f"{base}{path}", token=FARM_TOKEN, body=body or {})
            assert status == 401, path
            status, user_denied, _ = _http(method, f"{base}{path}", token=user_token, body=body or {})
            assert status == 403 and user_denied["error"] == "admin_forbidden", path

        status, verify_denied, _ = _http(
            "POST", f"{base}/farm/slots/2/cleanup-verified", token=admin_token, body={}
        )
        assert status == 401
        assert cleanup.is_required(2) is True
    finally:
        server.shutdown()


def test_access_log_redacts_session_query():
    mod = _load_mod()
    raw = '127.0.0.1 - "GET /admin/devices/3/live-access/stream?session=aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee HTTP/1.1" 200 -'
    redacted = mod._SESSION_QUERY_RE.sub(r"\1[redacted]", raw)
    assert "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee" not in redacted
    assert "session=[redacted]" in redacted


def test_routes_do_not_collide_with_customer_remote_access():
    rental = str(uuid.uuid4())
    assert parse_route(f"/rentals/{rental}/remote-access").kind == "remote_access"
    assert parse_route("/admin/devices/3/live-access").kind == "admin_live_access"
    assert parse_route("/admin/devices/3/live-access/reboot") is None
