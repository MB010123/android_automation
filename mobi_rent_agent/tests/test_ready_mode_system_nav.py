"""Ready-mode system navigation: Home/Recents/shade/QS after Phone Ready."""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infrastructure.gads_remote_access import GadsHubClient, GadsRemoteAccessPlatform
from tests.fakes_supabase import MemoryTenant
from tests.test_in_app_all_bays import _all_bay_service, _all_serials, _assert_no_secrets
from tests.test_remote_access_poc import (
    CUSTOMER_A,
    CUSTOMER_B,
    FakeFarm,
    FakePlatform,
    SLOT1_SERIAL,
    _Resp,
    _gads_multi,
    _rental,
    _service,
)

READY_ACTIONS = (
    ("home", None),
    ("recents", None),
    ("notification_shade", None),
    ("quick_settings", None),
    ("rotate", {"orientation": "portrait"}),
)


def _mark_ready(store, rental: str) -> None:
    store.set_activation_observed(rental, "confirmed")
    session = store.get(rental)
    store.upsert(replace(session, setup_phase="complete", setup_complete=True))


def _body(action: str, extra: dict | None) -> dict:
    payload = {"action": action}
    if extra:
        payload.update(extra)
    return payload


def test_setup_mode_allows_home_recents_shade_and_unrelated_apps(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    farm.inspect_allowed = False
    farm.inspect_activity = "com.android.chrome/.MainActivity"
    platform = FakePlatform()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    created = service.create_remote_access(CUSTOMER_A, None, rental)
    assert created.http_status == 201
    assert "home" in created.body["allowed_controls"]
    assert "denied_controls" not in created.body
    for action, extra in READY_ACTIONS:
        result = service.control_session(CUSTOMER_A, rental, _body(action, extra))
        assert result.http_status == 200, (action, result.body)
        assert result.body["ok"] is True
        _assert_no_secrets(result.body)
    tap = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 40, "y": 80})
    assert tap.http_status == 200
    assert {c[0] for c in platform.calls} >= {
        "home",
        "recents",
        "notification_shade",
        "quick_settings",
        "rotate",
        "tap",
    }
    assert not any(t["payload"].get("recover") for t in farm.tasks if t["type"] == "setup_session_inspect")


def test_phone_ready_allows_system_nav_commands(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform()
    service, store, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=FakeFarm())
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    created = service.create_remote_access(CUSTOMER_A, None, rental)
    assert "home" in created.body["allowed_controls"]
    assert "notification_shade" in created.body["allowed_controls"]
    assert "denied_controls" not in created.body
    _mark_ready(store, rental)
    got = service.get_remote_access(CUSTOMER_A, None, rental)
    assert got.body["setup_mode"] is False
    assert "notification_shade" in got.body["allowed_controls"]
    assert "denied_controls" not in got.body
    for action, extra in READY_ACTIONS:
        result = service.control_session(CUSTOMER_A, rental, _body(action, extra))
        assert result.http_status == 200, (action, result.body)
        assert result.body["ok"] is True
        assert result.body["forwarded"] is True
        assert result.body["action"] == action
    shade_swipe = service.control_session(
        CUSTOMER_A, rental, {"action": "swipe", "x": 12, "y": 8, "x2": 12, "y2": 400}
    )
    assert shade_swipe.http_status == 200
    assert {c[0] for c in platform.calls} >= {action for action, _ in READY_ACTIONS}


def test_ownership_assigned_slot_and_cross_rental_rejected(tmp_path: Path):
    tenant = MemoryTenant()
    service, store, _ = _gads_multi(tmp_path, tenant, farm=FakeFarm())
    rental_a = _rental(tenant, bay=8, user_id=CUSTOMER_A)
    rental_b = _rental(tenant, bay=9, user_id=CUSTOMER_B)
    assert service.create_remote_access(CUSTOMER_A, None, rental_a).http_status == 201
    assert service.create_remote_access(CUSTOMER_B, None, rental_b).http_status == 201
    _mark_ready(store, rental_a)
    stolen = service.control_session(CUSTOMER_B, rental_a, {"action": "home"})
    assert stolen.http_status == 403
    assert stolen.body["error"] == "rental_not_owned"
    cross = service.control_session(CUSTOMER_A, rental_b, {"action": "home"})
    assert cross.http_status == 403
    own_setup = service.control_session(CUSTOMER_B, rental_b, {"action": "home"})
    assert own_setup.http_status == 200
    assert own_setup.body["ok"] is True


def test_customer_cannot_target_another_device(tmp_path: Path):
    tenant = MemoryTenant()
    service, store, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=FakeFarm())
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    _mark_ready(store, rental)
    for key, value in (
        ("slot_id", 2),
        ("serial", "SERIAL-OTHER"),
        ("udid", "UDID-OTHER"),
        ("workspace_id", "ws-other"),
    ):
        denied = service.control_session(CUSTOMER_A, rental, {"action": "home", key: value})
        assert denied.http_status == 403, key
        assert denied.body["error"] == "forbidden_control"


def test_farm_timeout_is_not_fake_success(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform()
    platform.gads_nav_available = False
    farm = FakeFarm()
    farm.input_timeout = True
    service, store, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    _mark_ready(store, rental)
    home = service.control_session(CUSTOMER_A, rental, {"action": "home"})
    assert home.http_status == 504
    assert home.body["error"] == "timeout"
    assert home.body.get("ok") is False
    farm.input_timeout = False
    farm.fail_types.add("setup_session_input")
    shade = service.control_session(CUSTOMER_A, rental, {"action": "notification_shade"})
    assert shade.http_status == 502
    assert shade.body["error"] == "farm_unreachable"
    assert shade.body.get("ok") is False
    platform.gads_rotation_available = False
    rotated = service.control_session(
        CUSTOMER_A, rental, {"action": "rotate", "orientation": "landscape"}
    )
    assert rotated.http_status == 502
    assert rotated.body["error"] == "gads_unavailable"


def test_twenty_slots_ready_nav_with_fakes(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform(registered=set(_all_serials().values()))
    service, store, _ = _all_bay_service(tmp_path, tenant=tenant, platform=platform)
    for bay in range(1, 21):
        owner = CUSTOMER_A if bay % 2 else CUSTOMER_B
        rental = _rental(tenant, bay=bay, user_id=owner)
        created = service.create_remote_access(owner, None, rental)
        assert created.http_status == 201, (bay, created.body)
        before_ready = service.control_session(owner, rental, {"action": "home"})
        assert before_ready.http_status == 200, (bay, before_ready.body)
        _mark_ready(store, rental)
        home = service.control_session(owner, rental, {"action": "home"})
        recents = service.control_session(owner, rental, {"action": "recents"})
        shade = service.control_session(owner, rental, {"action": "notification_shade"})
        qs = service.control_session(owner, rental, {"action": "quick_settings"})
        assert home.http_status == 200, (bay, home.body)
        assert recents.http_status == 200 and shade.http_status == 200 and qs.http_status == 200
        other = CUSTOMER_B if owner == CUSTOMER_A else CUSTOMER_A
        stolen = service.control_session(other, rental, {"action": "home"})
        assert stolen.http_status == 403
        _assert_no_secrets(home.body)


def test_gads_maps_nav_to_known_suffixes_not_customer_shell():
    seen: list[tuple[str, str]] = []

    class Session:
        def request(self, method, url, json=None, params=None, headers=None, timeout=None):
            if url.endswith("/authenticate"):
                return _Resp(200, {"success": True, "result": {"access_token": "jwt-user"}})
            seen.append((method, url))
            if url.endswith("/notifications") or url.endswith("/quickSettings") or url.endswith("/rotation"):
                return _Resp(200, {"success": True})
            if url.endswith("/home") or url.endswith("/recents") or url.endswith("/recentApps"):
                return _Resp(200, {"success": True})
            return _Resp(404, {"success": False})

    client = GadsHubClient("http://hub", admin_username="a", admin_password="b", session=Session())  # type: ignore[arg-type]
    platform = GadsRemoteAccessPlatform(
        client, workspace_id="ws", public_url="https://remote.example", clock=lambda: 1_000_000.0
    )
    kwargs = {
        "device_id": SLOT1_SERIAL,
        "platform_username": "rental-user",
        "platform_password": "secret",
    }
    assert platform.press_home(**kwargs) is True
    assert platform.press_recents(**kwargs) is True
    assert platform.press_notification_shade(**kwargs) is True
    assert platform.press_quick_settings(**kwargs) is True
    assert platform.set_rotation(**kwargs, orientation="portrait") is True
    paths = [url for _method, url in seen]
    assert any(url.endswith("/home") for url in paths)
    assert any(url.endswith("/recents") or url.endswith("/recentApps") for url in paths)
    assert any(url.endswith("/notifications") for url in paths)
    assert any(url.endswith("/quickSettings") for url in paths)
    assert any(url.endswith("/rotation") for url in paths)
    blob = json.dumps(paths)
    assert "keyevent" not in blob
    assert "shell" not in blob
    assert "wm" not in blob
