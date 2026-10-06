"""Customer GADS sessions: no exclusive device lock, reuse, isolation."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infrastructure.gads_remote_access import GadsHubClient, GadsRemoteAccessPlatform
from tests.fakes_supabase import MemoryTenant
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


def test_customer_start_does_not_call_gads_device_lock():
    seen: list[str] = []

    class Session:
        def request(self, method, url, json=None, params=None, headers=None, timeout=None):
            seen.append(url)
            if url.endswith("/authenticate"):
                who = (json or {}).get("username")
                return _Resp(200, {"success": True, "result": {"access_token": f"jwt-{who}"}})
            if url.endswith("/admin/devices"):
                return _Resp(
                    200,
                    {"success": True, "result": {"devices": [{"udid": SLOT1_SERIAL, "workspace_id": "ws"}]}},
                )
            if url.endswith("/admin/user") and method == "POST":
                return _Resp(200, {"success": True})
            if "/admin/user/" in url and method == "DELETE":
                return _Resp(404, {"success": False})
            raise AssertionError(url)

    client = GadsHubClient("http://hub", admin_username="a", admin_password="b", session=Session())  # type: ignore[arg-type]
    platform = GadsRemoteAccessPlatform(
        client, workspace_id="ws", public_url="https://remote.example", clock=lambda: 1_000_000.0
    )
    grant = platform.grant_access(device_id=SLOT1_SERIAL, rental_id="abcd1234-0000", ttl_minutes=30)
    assert grant.platform_username == "rental-abcd12340000"
    assert grant.expires_at == 1_000_000.0 + 30 * 60
    assert not any(url.endswith("/lock") or "/unlock" in url for url in seen)


def test_reconnect_does_not_grant_or_lock_again(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=FakeFarm())
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    first = service.create_remote_access(CUSTOMER_A, None, rental)
    assert first.http_status == 201
    grants = [c for c in platform.calls if c[0] == "grant"]
    reused = service.create_remote_access(CUSTOMER_A, None, rental)
    assert reused.http_status == 200
    assert reused.body.get("active") is True
    assert [c for c in platform.calls if c[0] == "grant"] == grants
    assert [c for c in platform.calls if c[0] == "revoke"] == []
    assert SLOT1_SERIAL not in str(reused.body)


def test_customer_can_view_screen_and_use_approved_controls(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=FakeFarm())
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    stream = service.open_stream(CUSTOMER_A, rental)
    assert not hasattr(stream, "http_status")
    assert stream.status_code == 200
    tap = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 40, "y": 80})
    swipe = service.control_session(
        CUSTOMER_A, rental, {"action": "swipe", "x": 10, "y": 20, "x2": 30, "y2": 40}
    )
    typed = service.control_session(CUSTOMER_A, rental, {"action": "type", "text": "ok"})
    back = service.control_session(CUSTOMER_A, rental, {"action": "back"})
    assert tap.http_status == 200 and swipe.http_status == 200
    assert typed.http_status == 200 and back.http_status == 200
    assert {c[0] for c in platform.calls} >= {"grant", "stream", "tap", "swipe", "type", "back"}
    assert service.control_session(CUSTOMER_A, rental, {"action": "home"}).http_status == 403


def test_unauthorized_rental_and_cross_slot_access_rejected(tmp_path: Path):
    tenant = MemoryTenant()
    service, _, _ = _gads_multi(tmp_path, tenant, farm=FakeFarm())
    rental_a = _rental(tenant, bay=8, user_id=CUSTOMER_A)
    rental_b = _rental(tenant, bay=9, user_id=CUSTOMER_B)
    assert service.create_remote_access(CUSTOMER_A, None, rental_a).http_status == 201
    assert service.create_remote_access(CUSTOMER_B, None, rental_a).http_status == 403
    assert service.open_stream(CUSTOMER_B, rental_a).http_status == 403
    assert service.control_session(CUSTOMER_B, rental_a, {"action": "tap", "x": 1, "y": 1}).http_status == 403
    assert service.control_session(CUSTOMER_A, rental_b, {"action": "tap", "x": 1, "y": 1}).http_status == 403
    stolen = service.create_remote_access(CUSTOMER_A, None, rental_b)
    assert stolen.http_status == 403
    assert SLOT1_SERIAL not in str(stolen.body)


def test_two_slots_operate_independently(tmp_path: Path):
    tenant = MemoryTenant()
    service, _, _ = _gads_multi(tmp_path, tenant, farm=FakeFarm())
    r8 = _rental(tenant, bay=8, user_id=CUSTOMER_A)
    r9 = _rental(tenant, bay=9, user_id=CUSTOMER_B)
    a = service.create_remote_access(CUSTOMER_A, None, r8)
    b = service.create_remote_access(CUSTOMER_B, None, r9)
    assert a.http_status == 201 and b.http_status == 201
    assert service.control_session(CUSTOMER_A, r8, {"action": "tap", "x": 2, "y": 3}).http_status == 200
    assert service.control_session(CUSTOMER_B, r9, {"action": "back"}).http_status == 200
    grants = [c[1] for c in service._platform.calls if c[0] == "grant"]  # type: ignore[union-attr]
    assert {g["workspace_id"] for g in grants} == {"ws-8", "ws-9"}
    assert service.control_session(CUSTOMER_A, r9, {"action": "tap", "x": 1, "y": 1}).http_status == 403
    assert service.control_session(CUSTOMER_B, r8, {"action": "tap", "x": 1, "y": 1}).http_status == 403
