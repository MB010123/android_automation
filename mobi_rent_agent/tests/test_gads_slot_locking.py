"""Per-slot GADS locking, session reuse, and transient busy retry."""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from domain.remote_access import RemoteAccessPlatformError
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


class HoldingPlatform(FakePlatform):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.fail_next_tap = False
        self.tap_calls = 0
        self.grant_busy_remaining = 0

    def grant_access(self, *, device_id: str, rental_id: str, ttl_minutes: int, workspace_id: str = ""):
        if self.grant_busy_remaining > 0:
            self.grant_busy_remaining -= 1
            self.calls.append(("grant_busy", {"device_id": device_id}))
            raise RemoteAccessPlatformError("device_busy")
        return super().grant_access(
            device_id=device_id, rental_id=rental_id, ttl_minutes=ttl_minutes, workspace_id=workspace_id
        )

    def tap(self, *, device_id: str, platform_username: str, platform_password: str, x: int, y: int) -> None:
        self.tap_calls += 1
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise RemoteAccessPlatformError("device_busy")
        if self.fail_next_tap:
            self.fail_next_tap = False
            raise RemoteAccessPlatformError("gads_tap_failed status=500")
        super().tap(
            device_id=device_id,
            platform_username=platform_username,
            platform_password=platform_password,
            x=x,
            y=y,
        )


def test_concurrent_gads_same_slot_single_grant(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=FakeFarm())
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    barrier = threading.Barrier(2)
    results: list[int] = []

    def worker() -> None:
        barrier.wait()
        results.append(service.create_remote_access(CUSTOMER_A, None, rental).http_status)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert sorted(results) in ([200, 201],)
    grants = [c for c in platform.calls if c[0] == "grant"]
    assert len(grants) == 1
    assert [c for c in platform.calls if c[0] == "revoke"] == []


def test_concurrent_gads_different_slots_do_not_block(tmp_path: Path):
    tenant = MemoryTenant()
    service, _, _ = _gads_multi(tmp_path, tenant)
    r1 = _rental(tenant, bay=8, user_id=CUSTOMER_A)
    r2 = _rental(tenant, bay=9, user_id=CUSTOMER_B)
    barrier = threading.Barrier(2)
    results: dict[int, int] = {}

    def worker(owner: str, rental: str, slot: int) -> None:
        barrier.wait()
        results[slot] = service.create_remote_access(owner, None, rental).http_status

    t1 = threading.Thread(target=worker, args=(CUSTOMER_A, r1, 8))
    t2 = threading.Thread(target=worker, args=(CUSTOMER_B, r2, 9))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert results == {8: 201, 9: 201}
    grants = [c for c in service._platform.calls if c[0] == "grant"]  # type: ignore[union-attr]
    assert {g[1]["workspace_id"] for g in grants} == {"ws-8", "ws-9"}


def test_gads_lock_retries_transient_busy_then_succeeds():
    seen: list[int] = []
    sleeps: list[float] = []

    class Session:
        def request(self, method, url, json=None, params=None, headers=None, timeout=None):
            if url.endswith("/lock"):
                seen.append(1)
                if len(seen) < 3:
                    return _Resp(409, {"success": False})
                return _Resp(200, {"udid": SLOT1_SERIAL, "expires_at_ms": 1_700_000_000_000})
            raise AssertionError(url)

    client = GadsHubClient(
        "http://hub",
        admin_username="a",
        admin_password="b",
        session=Session(),  # type: ignore[arg-type]
        sleeper=sleeps.append,
    )
    expires = client.lock_device(SLOT1_SERIAL, token="user-jwt", ttl_minutes=10)
    assert expires == 1_700_000_000_000
    assert len(seen) == 3
    assert sleeps == [0.2, 0.5]


def test_gads_lock_gives_up_after_bounded_busy_retries():
    seen: list[int] = []

    class Session:
        def request(self, method, url, json=None, params=None, headers=None, timeout=None):
            if url.endswith("/lock"):
                seen.append(1)
                return _Resp(409, {"success": False})
            raise AssertionError(url)

    client = GadsHubClient(
        "http://hub",
        admin_username="a",
        admin_password="b",
        session=Session(),  # type: ignore[arg-type]
        sleeper=lambda _: None,
    )
    with pytest.raises(RemoteAccessPlatformError, match="device_busy"):
        client.lock_device(SLOT1_SERIAL, token="user-jwt", ttl_minutes=10)
    assert len(seen) == 4


def test_gads_tap_does_not_retry_busy():
    seen: list[int] = []

    class Session:
        def request(self, method, url, json=None, params=None, headers=None, timeout=None):
            if url.endswith("/authenticate"):
                return _Resp(200, {"success": True, "result": {"access_token": "t"}})
            if "/tap" in url:
                seen.append(1)
                return _Resp(409, {"success": False})
            raise AssertionError(url)

    client = GadsHubClient("http://hub", admin_username="a", admin_password="b", session=Session())  # type: ignore[arg-type]
    platform = GadsRemoteAccessPlatform(client, workspace_id="ws", public_url="https://remote.example")
    with pytest.raises(RemoteAccessPlatformError, match="device_busy"):
        platform.tap(
            device_id=SLOT1_SERIAL,
            platform_username="rental-x",
            platform_password="pw",
            x=1,
            y=2,
        )
    assert seen == [1]


def test_slot_lock_released_after_successful_control(tmp_path: Path):
    tenant = MemoryTenant()
    platform = HoldingPlatform()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=FakeFarm())
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    first = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 10, "y": 20})
    second = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 11, "y": 21})
    assert first.http_status == 200 and second.http_status == 200
    assert platform.tap_calls == 2


def test_slot_lock_released_after_control_exception(tmp_path: Path):
    tenant = MemoryTenant()
    platform = HoldingPlatform()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=FakeFarm())
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    platform.fail_next_tap = True
    failed = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 10, "y": 20})
    assert failed.http_status == 502
    recovered = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 12, "y": 22})
    assert recovered.http_status == 200
    assert platform.tap_calls == 2


def test_duplicate_control_clicks_do_not_stack(tmp_path: Path):
    tenant = MemoryTenant()
    platform = HoldingPlatform()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=FakeFarm())
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    platform.release.clear()
    first_result: list[int] = []

    def first() -> None:
        first_result.append(service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 1, "y": 1}).http_status)

    thread = threading.Thread(target=first)
    thread.start()
    assert platform.entered.wait(timeout=2)
    second = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 2, "y": 2})
    platform.release.set()
    thread.join(timeout=5)
    assert first_result == [200]
    assert second.http_status == 409
    assert second.body["error"] == "phone_operation_busy"
    assert platform.tap_calls == 1


def test_transient_busy_on_create_maps_to_phone_operation_busy(tmp_path: Path):
    tenant = MemoryTenant()
    platform = HoldingPlatform()
    platform.grant_busy_remaining = 1
    service, _, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=FakeFarm())
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    result = service.create_remote_access(CUSTOMER_A, None, rental)
    assert result.http_status == 409
    assert result.body["error"] == "phone_operation_busy"
    assert "The phone is busy with another operation" in result.body["message"]
    assert SLOT1_SERIAL not in str(result.body)
