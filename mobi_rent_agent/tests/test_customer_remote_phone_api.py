"""Customer remote-phone backend contract (no frontend)."""
from __future__ import annotations

import json
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.farm_agent_tasks import FarmTaskRequest, execute_farm_task
from application.farm_task_executor import FarmTaskExecutorDeps
from domain.remote_access import RemoteDeviceStatus
from tests.fakes_supabase import MemoryTenant
from tests.test_farm_agent_tasks import FakeResolver, FakeRunner, _config
from tests.test_in_app_all_bays import CUSTOMER_BAYS, _all_bay_service, _all_serials, _all_workspaces
from tests.test_remote_access_poc import (
    CUSTOMER_A,
    CUSTOMER_B,
    FakeFarm,
    FakePlatform,
    SLOT1_SERIAL,
    SLOT2_SERIAL,
    _http,
    _rental,
    _service,
    _start_http,
    _signup,
)

SECRET_MARKERS = ("SERIAL-", "workspace_id", "one-time-secret", "admin-jwt", "password")


def _assert_safe(body: dict) -> None:
    blob = json.dumps(body)
    for marker in SECRET_MARKERS:
        assert marker not in blob, marker
    assert SLOT1_SERIAL not in blob
    assert SLOT2_SERIAL not in blob


def test_create_reuses_session_and_hides_internals(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=FakeFarm())
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    first = service.create_remote_access(CUSTOMER_A, None, rental)
    assert first.http_status == 201, first.body
    _assert_safe(first.body)
    assert first.body["stream_path"].endswith("/remote-access/stream")
    assert first.body["allowed_controls"] == [
        "tap",
        "swipe",
        "type",
        "back",
        "home",
        "recents",
        "notification_shade",
        "quick_settings",
        "rotate",
    ]
    assert first.body["setup_mode"] is True
    assert "denied_controls" not in first.body
    assert first.body["coordinate_space"] == "native_device_pixels"
    grants = [c for c in platform.calls if c[0] == "grant"]
    reused = service.create_remote_access(CUSTOMER_A, None, rental)
    assert reused.http_status == 200
    assert reused.body["active"] is True
    assert [c for c in platform.calls if c[0] == "grant"] == grants


def test_duplicate_create_does_not_duplicate_gads_session(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=platform)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)

    def _create():
        return service.create_remote_access(CUSTOMER_A, None, rental).http_status

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(lambda _: _create(), range(8)))
    assert set(codes) <= {200, 201}
    assert codes.count(201) == 1
    assert [c for c in platform.calls if c[0] == "grant"]


def test_stream_uses_existing_session_and_control_is_independent(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform()
    service, store, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=FakeFarm())
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    grants_before = [c for c in platform.calls if c[0] == "grant"]
    stream = service.open_stream(CUSTOMER_A, rental)
    assert getattr(stream, "device_id") == SLOT1_SERIAL
    tap = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 10, "y": 20})
    assert tap.http_status == 200
    swipe = service.control_session(
        CUSTOMER_A,
        rental,
        {"action": "swipe", "start_x": 10, "start_y": 400, "end_x": 10, "end_y": 200, "duration_ms": 250},
    )
    typed = service.control_session(CUSTOMER_A, rental, {"action": "type", "text": "hi"})
    back = service.control_session(CUSTOMER_A, rental, {"action": "back"})
    home = service.control_session(CUSTOMER_A, rental, {"action": "home"})
    recents = service.control_session(CUSTOMER_A, rental, {"action": "recents"})
    edge = service.control_session(
        CUSTOMER_A, rental, {"action": "swipe", "x": 720, "y": 2900, "x2": 720, "y2": 2000}
    )
    assert swipe.http_status == 200 and typed.http_status == 200 and back.http_status == 200
    assert home.http_status == 200 and recents.http_status == 200 and edge.http_status == 200
    session = store.get(rental)
    store.set_activation_observed(rental, "confirmed")
    session = store.get(rental)
    store.upsert(replace(session, setup_phase="complete", setup_complete=True))
    home = service.control_session(CUSTOMER_A, rental, {"action": "home"})
    recents = service.control_session(CUSTOMER_A, rental, {"action": "recents"})
    shade_cmd = service.control_session(CUSTOMER_A, rental, {"action": "notification_shade"})
    qs = service.control_session(CUSTOMER_A, rental, {"action": "quick_settings"})
    shade_swipe = service.control_session(
        CUSTOMER_A, rental, {"action": "swipe", "x": 10, "y": 5, "x2": 10, "y2": 400}
    )
    rotated = service.control_session(
        CUSTOMER_A, rental, {"action": "rotate", "orientation": "portrait"}
    )
    assert home.http_status == 200 and recents.http_status == 200
    assert shade_cmd.http_status == 200 and qs.http_status == 200
    assert shade_swipe.http_status == 200 and rotated.http_status == 200
    assert {c[0] for c in platform.calls} >= {
        "tap",
        "swipe",
        "type",
        "back",
        "home",
        "recents",
        "notification_shade",
        "quick_settings",
        "rotate",
    }
    assert store.get(rental).is_active(1_000_000.0)
    assert [c for c in platform.calls if c[0] == "grant"] == grants_before
    swipe_calls = [c for c in platform.calls if c[0] == "swipe"]
    assert swipe_calls[0][1]["duration_ms"] == 250
    assert any(c[1]["y"] == 2900 and c[1]["y2"] == 2000 for c in swipe_calls)
    assert any(c[1]["y"] == 5 and c[1]["y2"] == 400 for c in swipe_calls)


def test_stream_and_control_concurrently(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=FakeFarm())
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    errors: list[str] = []

    def _stream():
        opened = service.open_stream(CUSTOMER_A, rental)
        if getattr(opened, "http_status", 200) != 200 and not getattr(opened, "device_id", None):
            errors.append("stream")

    def _control():
        result = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 5, "y": 6})
        if result.http_status != 200:
            errors.append(str(result.body))

    threads = [threading.Thread(target=_stream) for _ in range(4)] + [
        threading.Thread(target=_control) for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert any(c[0] == "tap" for c in platform.calls)
    assert any(c[0] == "stream" for c in platform.calls)


def test_coordinate_bounds_and_native_mapping_contract(tmp_path: Path):
    tenant = MemoryTenant()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform())
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    invalid = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": -1, "y": 10})
    assert invalid.http_status == 422
    assert invalid.body["error"] == "invalid_control"
    huge = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 9000, "y": 10})
    assert huge.http_status == 422
    created = service.get_remote_access(CUSTOMER_A, None, rental)
    assert created.body["coordinate_space"] == "native_device_pixels"
    status = service.device_status_for_customer(CUSTOMER_A, rental)
    assert status.body["coordinate_space"] == "native_device_pixels"


def test_device_status_does_not_mark_own_stream_busy(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform()
    service, store, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=FakeFarm())
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    store.set_prepare_state(rental, "ready", detail="customer_can_install_esim_manually")
    service.open_stream(CUSTOMER_A, rental)
    status = service.device_status_for_customer(CUSTOMER_A, rental)
    assert status.http_status == 200
    assert status.body["state"] == "online"
    assert status.body["remote_access_busy"] is False
    assert status.body["busy"] is False
    assert status.body["remote_access_available"] is True
    assert status.body["requires_manual_action"] is True
    assert status.body["session_active"] is True
    _assert_safe(status.body)


def test_requires_manual_action_does_not_block_remote_access(tmp_path: Path):
    tenant = MemoryTenant()
    service, store, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=FakeFarm())
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    created = service.create_remote_access(CUSTOMER_A, None, rental)
    assert created.http_status == 201
    store.set_prepare_state(rental, "ready")
    tap = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 12, "y": 40})
    stream = service.open_stream(CUSTOMER_A, rental)
    assert tap.http_status == 200
    assert getattr(stream, "device_id") == SLOT1_SERIAL


def test_phone_offline_is_not_busy(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform()
    platform.online[SLOT1_SERIAL] = False
    service, _, _ = _service(
        tmp_path,
        tenant=tenant,
        platform=platform,
        farm_status=lambda: {"ok": True, "offline_slots": [1], "mapped_slots": [1], "slot_count": 1},
    )
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    result = service.create_remote_access(CUSTOMER_A, None, rental)
    assert result.http_status == 409
    assert result.body["error"] == "phone_offline"
    assert result.body["error"] != "remote_access_busy"


def test_cross_rental_and_cross_slot_authorization(tmp_path: Path):
    tenant = MemoryTenant()
    service, _, _ = _service(
        tmp_path,
        tenant=tenant,
        platform=FakePlatform(registered={SLOT1_SERIAL, SLOT2_SERIAL}),
        allowed=(1, 2),
        workspace_map={1: "ws-1", 2: "ws-2"},
    )
    rental_a = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    rental_b = _rental(tenant, bay=2, user_id=CUSTOMER_B)
    assert service.create_remote_access(CUSTOMER_A, None, rental_a).http_status == 201
    assert service.create_remote_access(CUSTOMER_B, None, rental_b).http_status == 201
    stolen = service.control_session(CUSTOMER_B, rental_a, {"action": "tap", "x": 1, "y": 1})
    assert stolen.http_status == 403
    assert stolen.body["error"] == "rental_not_owned"
    assert service.open_stream(CUSTOMER_A, rental_b).http_status == 403
    assert service.create_remote_access(CUSTOMER_A, 2, rental_a).http_status == 403
    missing = service.create_remote_access(CUSTOMER_A, None, str(uuid.uuid4()))
    assert missing.http_status == 404
    assert missing.body["error"] == "rental_not_found"


def test_same_slot_busy_different_slots_independent(tmp_path: Path):
    tenant = MemoryTenant()
    service, _, _ = _service(
        tmp_path,
        tenant=tenant,
        platform=FakePlatform(registered={SLOT1_SERIAL, SLOT2_SERIAL}),
        allowed=(1, 2),
        workspace_map={1: "ws-1", 2: "ws-2"},
        farm=FakeFarm(),
    )
    first = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, first).http_status == 201
    second = str(uuid.uuid4())
    tenant.slots[1]["id"] = second
    tenant.slots[1]["rental_id"] = second
    tenant.slots[1]["user_id"] = CUSTOMER_B
    busy = service.create_remote_access(CUSTOMER_B, None, second)
    assert busy.http_status == 409
    assert busy.body["error"] == "remote_access_busy"
    rental_2 = _rental(tenant, bay=2, user_id=CUSTOMER_B)
    other = service.create_remote_access(CUSTOMER_B, None, rental_2)
    assert other.http_status == 201, other.body


def test_forbidden_controls_rejected(tmp_path: Path):
    tenant = MemoryTenant()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform())
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    shade = service.control_session(
        CUSTOMER_A, rental, {"action": "swipe", "x": 10, "y": 5, "x2": 10, "y2": 400}
    )
    adb = service.control_session(CUSTOMER_A, rental, {"action": "adb"})
    keys = service.control_session(CUSTOMER_A, rental, {"action": "keyevent", "keycode": 3})
    shell = service.control_session(CUSTOMER_A, rental, {"action": "shell"})
    spoof = service.control_session(
        CUSTOMER_A, rental, {"action": "home", "keycode": 3}
    )
    assert shade.http_status == 200
    assert {adb.body["error"], keys.body["error"], shell.body["error"], spoof.body["error"]} == {
        "forbidden_control"
    }
    assert keys.http_status == 403


def test_complete_releases_session_without_deleting_esim(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    platform = FakePlatform()
    service, store, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    done = service.complete_setup(CUSTOMER_A, rental)
    assert done.http_status == 200
    assert done.body["remote_session"] == "closed"
    assert done.body["esim_deleted"] is False
    assert done.body["factory_reset"] is False
    assert done.body["setup_complete"] is False
    assert store.get(rental).status != "active"
    assert any(t["type"] == "setup_session_safe_cleanup" for t in farm.tasks)


def test_no_automatic_esim_provisioning_in_assign():
    source = (ROOT / "application" / "farm_task_executor.py").read_text(encoding="utf-8")
    assert "EuiccManager" not in source
    assert "provisioner.provision" not in source
    provisioner = type(
        "P",
        (),
        {"provision": staticmethod(lambda *a, **k: (_ for _ in ()).throw(AssertionError("provision")))},
    )()
    result = execute_farm_task(
        adb_path="adb",
        slot_map={1: "SERIAL-A"},
        request=FarmTaskRequest(
            job_id="job-1",
            task_type="assign",
            farm_slot_id=1,
            payload={"esim_qr_url": "https://example.com/qr.png"},
        ),
        agent_config=_config(),
        deps=FarmTaskExecutorDeps(
            command_runner=FakeRunner(),
            provisioner=provisioner,  # type: ignore[arg-type]
            payload_resolver=FakeResolver(),
        ),
    )
    assert result.ok is False
    assert result.activation_code_sent is False


def test_qr_upload_places_image_without_gads_session(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    from tests.test_esim_qr_upload import PNG_BYTES

    placed = service.upload_esim_qr(CUSTOMER_A, rental, PNG_BYTES)
    assert placed.http_status == 200
    assert placed.body["placed"] is True
    assert placed.body["remote_size"] == len(PNG_BYTES)
    assert "serial" not in placed.body
    assert farm.tasks[0]["type"] == "remote_access_place_qr"


def test_http_stream_auth_and_get_device_status(tmp_path: Path):
    server, port, tenant, platform, Handler = _start_http(tmp_path)
    base = f"http://127.0.0.1:{port}"
    try:
        token_a, user_a = _signup(base, "ra-a@example.com")
        token_b, _user_b = _signup(base, "ra-b@example.com")
        rental = _rental(tenant, bay=1, user_id=user_a)
        status, created, _ = _http("POST", f"{base}/rentals/{rental}/remote-access", token=token_a, body={})
        assert status == 201, created
        status, _, _ = _http("GET", f"{base}{created['stream_path']}")
        assert status == 401
        status, _, _ = _http("GET", f"{base}{created['stream_path']}", token=token_b)
        assert status == 403
        import urllib.request

        req = urllib.request.Request(
            f"{base}{created['stream_path']}",
            method="GET",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            assert resp.status == 200
            ctype = resp.headers.get("Content-Type") or ""
            assert "multipart/x-mixed-replace" in ctype
            assert "boundary=" in ctype.lower()
            chunk = resp.read(64)
            assert chunk.startswith(b"--")
        status, ds, _ = _http("GET", f"{base}/rentals/{rental}/remote-access/device-status", token=token_a)
        assert status == 200, ds
        assert status != 405
        assert ds["remote_access_busy"] is False
        assert ds["state"] == "online"
        assert ds["native_resolution"] == {"width": 1440, "height": 3120}
        status, posted, _ = _http(
            "POST", f"{base}/rentals/{rental}/remote-access/device-status", token=token_a, body={}
        )
        assert status == 200
        assert posted["native_resolution"] == ds["native_resolution"]
        status, tap, _ = _http(
            "POST",
            f"{base}/rentals/{rental}/remote-access/control",
            token=token_a,
            body={"action": "tap", "x": 40, "y": 80},
        )
        assert status == 200, tap
        assert any(c[0] == "tap" for c in platform.calls)
        status, home, _ = _http(
            "POST",
            f"{base}/rentals/{rental}/remote-access/control",
            token=token_a,
            body={"action": "home"},
        )
        assert status == 200, home
        status, recents, _ = _http(
            "POST",
            f"{base}/rentals/{rental}/remote-access/control",
            token=token_a,
            body={"action": "recents"},
        )
        assert status == 200, recents
        store = Handler.remote_access_service._store
        store.set_activation_observed(rental, "confirmed")
        session = store.get(rental)
        store.upsert(replace(session, setup_phase="complete", setup_complete=True))
        status, home, _ = _http(
            "POST",
            f"{base}/rentals/{rental}/remote-access/control",
            token=token_a,
            body={"action": "home"},
        )
        assert status == 200, home
        status, recents, _ = _http(
            "POST",
            f"{base}/rentals/{rental}/remote-access/control",
            token=token_a,
            body={"action": "recents"},
        )
        assert status == 200, recents
        status, edge, _ = _http(
            "POST",
            f"{base}/rentals/{rental}/remote-access/control",
            token=token_a,
            body={"action": "swipe", "x": 720, "y": 2900, "x2": 720, "y2": 2000},
        )
        assert status == 200, edge
        stolen = _http(
            "POST",
            f"{base}/rentals/{rental}/remote-access/control",
            token=token_b,
            body={"action": "home"},
        )
        assert stolen[0] == 403
        keys = _http(
            "POST",
            f"{base}/rentals/{rental}/remote-access/control",
            token=token_a,
            body={"action": "keyevent", "keycode": 3},
        )
        assert keys[0] == 403
        status, done, _ = _http(
            "POST", f"{base}/rentals/{rental}/remote-access/complete", token=token_a, body={}
        )
        assert status == 200, done
        assert done["remote_session"] == "closed"
    finally:
        server.shutdown()


def test_gads_own_in_use_is_not_customer_busy():
    pub = RemoteDeviceStatus(
        slot_id=1,
        device_id=SLOT1_SERIAL,
        registered=True,
        online=True,
        available=False,
        in_use_by="other-internal",
        screen_width=1440,
        screen_height=3120,
    ).to_public_dict()
    assert pub["busy"] is False
    assert pub["native_resolution"] == {"width": 1440, "height": 3120}


def test_twenty_slot_workspaces_remain_unique(tmp_path: Path):
    tenant = MemoryTenant()
    service, _, _ = _all_bay_service(tmp_path, tenant=tenant)
    serials = _all_serials()
    workspaces = _all_workspaces()
    assert len(set(workspaces.values())) == 20
    for bay in CUSTOMER_BAYS:
        rental = _rental(tenant, bay=bay, user_id=CUSTOMER_A if bay != 15 else CUSTOMER_B)
        owner = CUSTOMER_A if bay != 15 else CUSTOMER_B
        created = service.create_remote_access(owner, None, rental)
        assert created.http_status == 201, (bay, created.body)
        _assert_safe(created.body)
        grants = [c for c in service._platform.calls if c[0] == "grant"]  # type: ignore[union-attr]
        assert grants[-1][1]["workspace_id"] == workspaces[bay]
        assert grants[-1][1]["device_id"] == serials[bay]
        assert service.control_session(owner, rental, {"action": "back"}).http_status == 200
        stream = service.open_stream(owner, rental)
        assert getattr(stream, "device_id") == serials[bay]


def test_parse_wm_size_uses_physical_not_a_default():
    from application.device_display_size import parse_wm_size

    assert parse_wm_size("Physical size: 1440x3120\n") == (1440, 3120)
    assert parse_wm_size("Physical size: 1344x2992\nOverride size: 720x1600\n") == (1344, 2992)
    assert parse_wm_size("") is None
    assert parse_wm_size("Physical size: 0x0\n") is None


def test_get_device_status_returns_farm_wm_size_not_stream_frame(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    farm.display_width = 1344
    farm.display_height = 2992
    service, _, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    status = service.device_status_for_customer(CUSTOMER_A, rental)
    assert status.http_status == 200
    assert status.body["native_resolution"] == {"width": 1344, "height": 2992}
    assert status.body["native_resolution"] != {"width": 720, "height": 1600}
    assert status.body["native_resolution"] != {"width": 1080, "height": 2400}
    assert status.body["native_resolution"] != {"width": 1080, "height": 2220}
    assert "native_resolution_unavailable" not in status.body
    sized = [t for t in farm.tasks if t["type"] == "device_display_size"]
    assert len(sized) == 1
    again = service.device_status_for_customer(CUSTOMER_A, rental)
    assert again.body["native_resolution"] == {"width": 1344, "height": 2992}
    assert len([t for t in farm.tasks if t["type"] == "device_display_size"]) == 1


def test_get_device_status_omits_invented_resolution_when_wm_size_missing(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    farm.fail_types.add("device_display_size")
    service, _, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    status = service.device_status_for_customer(CUSTOMER_A, rental)
    assert status.http_status == 200
    assert "native_resolution" not in status.body
    assert status.body["native_resolution_unavailable"] == "wm_size_unavailable"


def test_http_get_device_status_is_not_405_and_enforces_ownership(tmp_path: Path):
    server, port, tenant, _platform, _ = _start_http(tmp_path)
    base = f"http://127.0.0.1:{port}"
    try:
        token_a, user_a = _signup(base, "ds-a@example.com")
        token_b, _user_b = _signup(base, "ds-b@example.com")
        rental = _rental(tenant, bay=1, user_id=user_a)
        url = f"{base}/rentals/{rental}/remote-access/device-status"
        status, _, _ = _http("GET", url)
        assert status == 401
        status, created, _ = _http("POST", f"{base}/rentals/{rental}/remote-access", token=token_a, body={})
        assert status == 201, created
        status, body, _ = _http("GET", url, token=token_a)
        assert status == 200, body
        assert status != 405
        assert body["native_resolution"] == {"width": 1440, "height": 3120}
        stolen = _http("GET", url, token=token_b)
        assert stolen[0] == 403
        assert stolen[1]["error"] == "rental_not_owned"
        status, posted, _ = _http("POST", url, token=token_a, body={})
        assert status == 200
        assert posted["native_resolution"] == body["native_resolution"]
        assert posted["state"] == body["state"]
    finally:
        server.shutdown()


def test_device_display_size_task_reads_wm_size_from_adb():
    from infrastructure.adb_companion import AdbCommandResult

    class WmRunner(FakeRunner):
        def run(self, serial: str, arguments: list[str]) -> AdbCommandResult:
            if arguments == ["shell", "wm", "size"]:
                return AdbCommandResult(stdout="Physical size: 1440x3120\n", stderr="")
            return super().run(serial, arguments)

    result = execute_farm_task(
        adb_path="adb",
        slot_map={1: "SERIAL-A"},
        request=FarmTaskRequest(
            job_id="job-ds",
            task_type="device_display_size",
            farm_slot_id=1,
            payload={},
        ),
        agent_config=_config(),
        deps=FarmTaskExecutorDeps(command_runner=WmRunner()),
    )
    assert result.ok is True
    assert result.details == {"width": 1440, "height": 3120, "source": "wm_size_physical"}


def test_farm_nav_input_uses_fixed_internal_events():
    runner = FakeRunner()
    for kind, code in (("home", "3"), ("recents", "187"), ("back", "4")):
        runner.calls.clear()
        result = execute_farm_task(
            adb_path="adb",
            slot_map={2: "SERIAL-B"},
            request=FarmTaskRequest(
                job_id=f"job-{kind}",
                task_type="setup_session_input",
                farm_slot_id=2,
                payload={"kind": kind},
            ),
            agent_config=_config(),
            deps=FarmTaskExecutorDeps(command_runner=runner),
        )
        assert result.ok is True, kind
        assert runner.calls == [("SERIAL-B", ["shell", "input", "keyevent", code])]
    for kind, args in (
        ("notification_shade", ["cmd", "statusbar", "expand-notifications"]),
        ("quick_settings", ["cmd", "statusbar", "expand-settings"]),
    ):
        runner.calls.clear()
        result = execute_farm_task(
            adb_path="adb",
            slot_map={2: "SERIAL-B"},
            request=FarmTaskRequest(
                job_id=f"job-{kind}",
                task_type="setup_session_input",
                farm_slot_id=2,
                payload={"kind": kind},
            ),
            agent_config=_config(),
            deps=FarmTaskExecutorDeps(command_runner=runner),
        )
        assert result.ok is True, kind
        assert runner.calls == [("SERIAL-B", ["shell", *args])]
    denied = execute_farm_task(
        adb_path="adb",
        slot_map={2: "SERIAL-B"},
        request=FarmTaskRequest(
            job_id="job-shell",
            task_type="setup_session_input",
            farm_slot_id=2,
            payload={"kind": "shell"},
        ),
        agent_config=_config(),
        deps=FarmTaskExecutorDeps(command_runner=FakeRunner()),
    )
    assert denied.ok is False
    assert denied.error == "invalid_control"


def test_no_hardcoded_pixel_resolution_in_device_status_code():
    files = (
        ROOT / "application" / "remote_access_service.py",
        ROOT / "application" / "device_display_size.py",
        ROOT / "tools" / "vps_backend_server.py",
    )
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "1080x2400" not in text
        assert "1080x2220" not in text
        assert "width\": 1080" not in text
        assert "height\": 2400" not in text

