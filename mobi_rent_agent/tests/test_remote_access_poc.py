"""Slot-1 remote-access POC: authorization chain, GADS adapter, farm QR task, HTTP.

Tests A-K from the POC brief. Everything is mocked: no GADS hub, no ADB,
no Lovable server, no live device.
"""
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
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.auth_service import AuthService
from application.farm_agent_tasks import FarmTaskRequest, execute_farm_task, parse_farm_task_body
from application.farm_task_executor import FarmTaskExecutorDeps
from application.remote_access_farm_task import run_remote_access_place_qr
from application.remote_access_service import (
    PREPARE_FAILED,
    PREPARE_PLACING_QR,
    PREPARE_READY,
    PREPARE_REBOOTING,
    REMOTE_ACCESS_PLACE_QR_TASK,
    RemoteAccessService,
)
from application.vps_lovable_routes import parse_route
from domain.remote_access import PlatformAccessGrant, RemoteAccessPlatformError, RemoteDeviceStatus
from infrastructure.adb_companion import AdbCommandResult, AdbCommandRunner
from infrastructure.config import AgentConfig, load_config
from infrastructure.farm_task_client import FarmTaskResponse
from infrastructure.gads_remote_access import (
    GadsHubClient,
    GadsRemoteAccessPlatform,
    platform_username_for_rental,
)
from infrastructure.remote_access_store import STATUS_ACTIVE, RemoteAccessSessionStore
from tests.fakes_supabase import MemoryGoTrue, MemoryTenant, seed_owned_slot

SLOT1_SERIAL = "SERIAL-SLOT1-TEST"  # stands in for the gitignored slot_map.json entry
SLOT2_SERIAL = "SERIAL-SLOT2-TEST"
CUSTOMER_A = "customer-a"
CUSTOMER_B = "customer-b"
FARM_TOKEN = "farm-service-secret"
STRONG = "correct-horse-battery"

def _png_bytes() -> bytes:
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (8, 8), "white").save(buf, format="PNG")
    return buf.getvalue()


PNG_BYTES = _png_bytes()


class FakeMjpeg:
    def __init__(self, device_id: str) -> None:
        self.device_id = device_id
        self.status_code = 200
        self.headers = {"Content-Type": "multipart/x-mixed-replace; boundary=frame"}
        self.closed = False

    def iter_content(self, chunk_size: int = 8192):
        _ = chunk_size
        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\nxxxx\r\n"

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakePlatform:
    """In-memory stand-in for the GADS adapter."""

    def __init__(self, *, registered: set[str] | None = None) -> None:
        self.registered = registered or {SLOT1_SERIAL}
        self.leases: dict[str, str] = {}  # device_id -> username
        self.users: set[str] = set()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.online: dict[str, bool] = {serial: True for serial in self.registered}
        self.fail_grant: str | None = None
        self.fail_revoke = False
        self.device_workspace: dict[str, str] = {}
        self.gads_nav_available = True
        self.gads_rotation_available = True

    def grant_access(self, *, device_id: str, rental_id: str, ttl_minutes: int, workspace_id: str = "") -> PlatformAccessGrant:
        self.calls.append(("grant", {"device_id": device_id, "rental_id": rental_id, "ttl": ttl_minutes, "workspace_id": workspace_id}))
        if self.fail_grant:
            raise RemoteAccessPlatformError(self.fail_grant)
        if device_id not in self.registered:
            raise RemoteAccessPlatformError("device_not_registered")
        if workspace_id and self.device_workspace:
            if self.device_workspace.get(device_id) != workspace_id:
                raise RemoteAccessPlatformError("device_not_in_poc_workspace")
        username = platform_username_for_rental(rental_id)
        self.users.add(username)
        self.leases[device_id] = username
        return PlatformAccessGrant(
            device_id=device_id,
            platform_username=username,
            platform_password="one-time-secret-" + rental_id[:4],
            access_url="https://remote.example/",
            expires_at=1_000_000.0 + ttl_minutes * 60,
        )

    def revoke_access(self, *, device_id: str, platform_username: str) -> bool:
        self.calls.append(("revoke", {"device_id": device_id, "username": platform_username}))
        if self.fail_revoke:
            raise RemoteAccessPlatformError("gads_release_failed")
        self.users.discard(platform_username)
        if self.leases.get(device_id) == platform_username:
            del self.leases[device_id]
        return True

    def release_device(self, *, device_id: str) -> bool:
        self.calls.append(("release", {"device_id": device_id}))
        self.leases.pop(device_id, None)
        return True

    def device_status(self, *, slot_id: int, device_id: str, workspace_id: str | None = None) -> RemoteDeviceStatus:
        self.calls.append(("status", {"device_id": device_id, "workspace_id": workspace_id}))
        registered = device_id in self.registered
        online = registered and self.online.get(device_id, False)
        return RemoteDeviceStatus(
            slot_id=slot_id,
            device_id=device_id,
            registered=registered,
            online=online,
            available=online,
            in_use_by=self.leases.get(device_id),
        )

    def tap(self, *, device_id: str, platform_username: str, platform_password: str, x: int, y: int) -> None:
        self.calls.append(("tap", {"device_id": device_id, "x": x, "y": y, "username": platform_username}))
        if self.leases.get(device_id) != platform_username:
            raise RemoteAccessPlatformError("device_not_locked")

    def swipe(
        self,
        *,
        device_id: str,
        platform_username: str,
        platform_password: str,
        x: int,
        y: int,
        x2: int,
        y2: int,
        duration_ms: int | None = None,
    ) -> bool:
        self.calls.append(
            ("swipe", {"device_id": device_id, "x": x, "y": y, "x2": x2, "y2": y2, "duration_ms": duration_ms})
        )
        if self.leases.get(device_id) != platform_username:
            raise RemoteAccessPlatformError("device_not_locked")
        return True

    def type_text(self, *, device_id: str, platform_username: str, platform_password: str, text: str) -> None:
        self.calls.append(("type", {"device_id": device_id, "text": text}))
        if self.leases.get(device_id) != platform_username:
            raise RemoteAccessPlatformError("device_not_locked")

    def press_back(self, *, device_id: str, platform_username: str, platform_password: str) -> bool:
        self.calls.append(("back", {"device_id": device_id}))
        if self.leases.get(device_id) != platform_username:
            raise RemoteAccessPlatformError("device_not_locked")
        return bool(self.gads_nav_available)

    def press_home(self, *, device_id: str, platform_username: str, platform_password: str) -> bool:
        self.calls.append(("home", {"device_id": device_id}))
        if self.leases.get(device_id) != platform_username:
            raise RemoteAccessPlatformError("device_not_locked")
        return bool(self.gads_nav_available)

    def press_recents(self, *, device_id: str, platform_username: str, platform_password: str) -> bool:
        self.calls.append(("recents", {"device_id": device_id}))
        if self.leases.get(device_id) != platform_username:
            raise RemoteAccessPlatformError("device_not_locked")
        return bool(self.gads_nav_available)

    def press_notification_shade(
        self, *, device_id: str, platform_username: str, platform_password: str
    ) -> bool:
        self.calls.append(("notification_shade", {"device_id": device_id}))
        if self.leases.get(device_id) != platform_username:
            raise RemoteAccessPlatformError("device_not_locked")
        return bool(self.gads_nav_available)

    def press_quick_settings(
        self, *, device_id: str, platform_username: str, platform_password: str
    ) -> bool:
        self.calls.append(("quick_settings", {"device_id": device_id}))
        if self.leases.get(device_id) != platform_username:
            raise RemoteAccessPlatformError("device_not_locked")
        return bool(self.gads_nav_available)

    def set_rotation(
        self,
        *,
        device_id: str,
        platform_username: str,
        platform_password: str,
        orientation: str,
    ) -> bool:
        self.calls.append(("rotate", {"device_id": device_id, "orientation": orientation}))
        if self.leases.get(device_id) != platform_username:
            raise RemoteAccessPlatformError("device_not_locked")
        return bool(self.gads_rotation_available)

    def open_mjpeg_stream(self, *, device_id: str, platform_username: str, platform_password: str):
        self.calls.append(("stream", {"device_id": device_id, "username": platform_username}))
        if self.leases.get(device_id) != platform_username:
            raise RemoteAccessPlatformError("device_not_locked")
        return FakeMjpeg(device_id)


class FakeFarm:
    def __init__(self) -> None:
        self.tasks: list[dict[str, Any]] = []
        self.fail_types: set[str] = set()
        self.activation_details: dict[str, Any] | None = None
        self.inspect_allowed = True
        self.inspect_activity: str | None = None
        self.inspect_activities: list[str] = []
        self.inspect_unknown = False
        self.display_width = 1440
        self.display_height = 3120
        self.airplane_timeout = False
        self.reboot_timeout = False
        self.input_timeout = False

    def run_task(self, *, task_type: str, farm_slot_id: int, payload: dict, job_id: str) -> FarmTaskResponse:
        self.tasks.append({"type": task_type, "slot": farm_slot_id, "payload": payload, "job_id": job_id})
        if task_type == "airplane_cycle":
            if self.airplane_timeout:
                raise TimeoutError("farm_timeout")
            return FarmTaskResponse(
                ok=False,
                http_status=501,
                body={"error": "action_not_supported"},
                error="action_not_supported",
            )
        if task_type == "reboot" and self.reboot_timeout:
            raise TimeoutError("farm_timeout")
        if task_type == "setup_session_input" and self.input_timeout:
            raise TimeoutError("farm_timeout")
        if task_type in self.fail_types:
            return FarmTaskResponse(ok=False, http_status=422, body={"details": {"placed": False, "error_code": "reboot_failed"}}, error="reboot_failed")
        body: dict[str, Any] = {"ok": True}
        if task_type == "remote_access_place_qr":
            size = 1024
            raw = payload.get("image_base64")
            if isinstance(raw, str) and raw:
                import base64

                size = len(base64.b64decode(raw))
            dest = "/sdcard/DCIM/Camera/mobirent_esim_qr_placed.png"
            body["message"] = f"qr_placed:{dest};media_scanned=true"
            body["details"] = {
                "ok": True,
                "placed": True,
                "job_id": job_id,
                "serial": "MUST-NOT-LEAK-TO-BROWSER",
                "destination": dest,
                "downloaded_size": size,
                "remote_size": size,
                "error_code": None,
            }
        if task_type == "remote_access_activation_status" and self.activation_details:
            body["details"] = self.activation_details
        if task_type == "setup_session_inspect":
            allowed = bool(self.inspect_allowed) and not self.inspect_unknown
            recovered = bool(payload.get("recover")) and not allowed
            if self.inspect_unknown:
                activity = None
                reason = "activity_unknown"
            else:
                activity = self.inspect_activities.pop(0) if self.inspect_activities else self.inspect_activity
                if not activity:
                    activity = (
                        "com.android.settings/.network.telephony.MobileNetworkActivity"
                        if allowed
                        else "com.google.android.apps.nexuslauncher/.NexusLauncherActivity"
                    )
                reason = "ok" if allowed else "left_setup"
            if recovered:
                activity = "com.android.settings/.network.telephony.MobileNetworkActivity"
                allowed = True
                reason = "ok"
                self.inspect_activity = activity
                self.inspect_allowed = True
            body["details"] = {
                "activity": activity,
                "allowed": allowed,
                "recovered": recovered,
                "reason": reason,
                "phase": payload.get("phase") or "esim",
                "sms_role_holder": "com.voidfix.app",
                "voidfix_is_default_sms": True,
                "voidfix_running": True,
            }
        if task_type == "setup_session_voidfix_cycle":
            body["details"] = {
                "cycled": True,
                "voidfix_is_default_sms": True,
                "voidfix_running": True,
                "sms_role_holder": "com.voidfix.app",
            }
        if task_type == "setup_session_safe_cleanup":
            body["details"] = {"removed_qr_artifacts": True, "factory_reset": False, "esim_deleted": False}
        if task_type == "setup_session_input":
            body["details"] = {"ok": True}
        if task_type == "device_display_size":
            body["details"] = {
                "width": int(self.display_width),
                "height": int(self.display_height),
                "source": "wm_size_physical",
            }
        return FarmTaskResponse(ok=True, http_status=200, body=body)


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 1.0)


class FarmStatusSequence:
    """Farm /agent/health responses for reboot wait: offline then online."""

    def __init__(self, sequence: list[list[int]]) -> None:
        self._seq = list(sequence)

    def __call__(self) -> dict:
        offline = self._seq.pop(0) if len(self._seq) > 1 else self._seq[0]
        return {"ok": not offline, "offline_slots": offline, "mapped_slots": [1, 2], "slot_count": 2}


def _service(
    tmp_path: Path,
    *,
    tenant: MemoryTenant,
    platform: FakePlatform | None = None,
    farm: FakeFarm | None = None,
    enabled: bool = True,
    allowed: tuple[int, ...] = (1,),
    slot_map: dict[int, str] | None = None,
    clock: FakeClock | None = None,
    farm_status=None,
    ttl_minutes: int = 60,
    workspace_map: dict[int, str] | None = None,
    gads_slot_ids: tuple[int, ...] | None = None,
    prepare_slot_ids: tuple[int, ...] | None = (1,),
    observe_slot_ids: tuple[int, ...] | None = (1,),
    voidfix_package: str | None = None,
    session_restrictions: bool = True,
    slot_msisdn_map_path: str | None = None,
) -> tuple[RemoteAccessService, RemoteAccessSessionStore, FakeClock]:
    clock = clock or FakeClock()
    store = RemoteAccessSessionStore(tmp_path / f"ra-{uuid.uuid4().hex}.sqlite")
    if farm_status is None:
        farm_status = lambda: {  # noqa: E731
            "ok": True,
            "offline_slots": [],
            "mapped_slots": list(range(1, 21)),
            "slot_count": 20,
            "adb_online": 20,
        }
    service = RemoteAccessService(
        enabled=enabled,
        allowed_slot_ids=allowed,
        slot_device_map=slot_map if slot_map is not None else {1: SLOT1_SERIAL, 2: SLOT2_SERIAL},
        platform=platform,
        store=store,
        tenant_store=tenant,
        farm_task_client=farm,
        farm_status_fetcher=farm_status,
        esim_url_prefixes=("https://example.test/",),
        session_ttl_minutes=ttl_minutes,
        reboot_timeout_seconds=60.0,
        poll_interval_seconds=5.0,
        clock=clock,
        sleep=clock.sleep,
        background_runner=lambda fn: fn(),  # synchronous in tests
        workspace_map=workspace_map if workspace_map is not None else {1: "ws-poc"},
        gads_slot_ids=gads_slot_ids if gads_slot_ids is not None else allowed,
        prepare_slot_ids=prepare_slot_ids,
        observe_slot_ids=observe_slot_ids,
        voidfix_android_package=voidfix_package,
        session_restrictions=session_restrictions,
        slot_msisdn_map_path=slot_msisdn_map_path,
    )
    return service, store, clock


def _rental(tenant: MemoryTenant, *, bay: int, user_id: str, **extra) -> str:
    rental_id = str(uuid.uuid4())
    seed_owned_slot(tenant, bay=bay, rental_id=rental_id, user_id=user_id)
    tenant.slots[bay].update(extra)
    return rental_id


# ---------------------------------------------------------------------------
# A. Slot 1 can be mapped to the remote device
# ---------------------------------------------------------------------------


def test_a_slot1_maps_to_remote_device_via_slot_map(tmp_path: Path):
    tenant = MemoryTenant()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform())
    assert service.device_for_slot(1) == SLOT1_SERIAL
    # Slot 2 exists in the slot map but is outside the POC allowlist: never exposed.
    assert service.device_for_slot(2) is None


def test_a_gads_adapter_reports_slot1_registered_and_online():
    class Session:
        def __init__(self) -> None:
            self.requests: list[tuple[str, str]] = []

        def request(self, method, url, json=None, params=None, headers=None, timeout=None):
            self.requests.append((method, url))
            if url.endswith("/authenticate"):
                return _Resp(200, {"success": True, "result": {"access_token": "admin-jwt"}})
            if url.endswith("/admin/devices"):
                return _Resp(
                    200,
                    {
                        "success": True,
                        "result": {"devices": [{"udid": SLOT1_SERIAL, "workspace_id": "ws-poc", "os": "android"}]},
                    },
                )
            raise AssertionError(url)

        def get(self, url, params=None, headers=None, timeout=None, stream=False):
            assert url.endswith("/available-devices") and params == {"workspaceId": "ws-poc"}
            live = [{"info": {"udid": SLOT1_SERIAL}, "connected": True, "provider_state": "live", "available": True, "in_use": False, "in_use_by": ""}]
            return _SseResp(f"data:{json.dumps(live)}")

    session = Session()
    client = GadsHubClient("http://hub.local:10000", admin_username="admin", admin_password="pw", session=session)  # type: ignore[arg-type]
    platform = GadsRemoteAccessPlatform(client, workspace_id="ws-poc", public_url="https://remote.example")
    status = platform.device_status(slot_id=1, device_id=SLOT1_SERIAL)
    assert status.registered and status.online and status.available
    assert status.to_public_dict()["state"] == "online"
    other = platform.device_status(slot_id=1, device_id="SOME-OTHER-PHONE")
    assert not other.registered and other.to_public_dict()["state"] == "unregistered"


class _Resp:
    def __init__(self, status: int, body: dict) -> None:
        self.status_code = status
        self._body = body
        self.content = json.dumps(body).encode()

    def json(self):
        return self._body


class _SseResp:
    status_code = 200

    def __init__(self, text: str) -> None:
        self._text = text

    def iter_lines(self, decode_unicode=True):
        yield from self._text.splitlines()

    def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# B. Customer A can receive access to Slot 1
# ---------------------------------------------------------------------------


def test_b_customer_a_gets_slot1_access(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform()
    service, store, clock = _service(tmp_path, tenant=tenant, platform=platform)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)

    result = service.create_remote_access(CUSTOMER_A, None, rental)
    assert result.http_status == 201, result.body
    assert result.body["active"] is True
    assert result.body["slot_id"] == 1
    assert result.body["session_mode"] == "in_app"
    assert "platform_login" not in result.body
    assert "password" not in json.dumps(result.body)
    assert result.body["stream_path"] == f"/rentals/{rental}/remote-access/stream"
    assert platform.leases[SLOT1_SERIAL] == platform_username_for_rental(rental)

    got = service.get_remote_access(CUSTOMER_A, None, rental)
    assert got.http_status == 200 and got.body["active"] is True
    assert "platform_login" not in got.body
    assert SLOT1_SERIAL not in json.dumps(got.body)

    session = store.get(rental)
    assert session is not None and session.status == STATUS_ACTIVE and session.device_id == SLOT1_SERIAL


def test_b_explicit_slot_must_match_rental(tmp_path: Path):
    tenant = MemoryTenant()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform())
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, 2, rental).http_status == 403
    assert service.create_remote_access(CUSTOMER_A, 1, rental).http_status == 201


# ---------------------------------------------------------------------------
# C. Customer B cannot receive access to Slot 1 owned by Customer A
# ---------------------------------------------------------------------------


def test_c_customer_b_cannot_access_customer_a_slot1(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=platform)
    rental_a = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental_a).http_status == 201

    # B uses A's rental id -> 403, no platform call
    before = len(platform.calls)
    for call in (
        service.create_remote_access(CUSTOMER_B, None, rental_a),
        service.get_remote_access(CUSTOMER_B, None, rental_a),
        service.revoke_remote_access(CUSTOMER_B, None, rental_a),
        service.device_status_for_customer(CUSTOMER_B, rental_a),
        service.reboot_for_customer(CUSTOMER_B, rental_a),
        service.prepare_esim(CUSTOMER_B, rental_a),
    ):
        assert call.http_status == 403, call.body
        assert call.body["error"] == "rental_not_owned"
    assert len(platform.calls) == before
    assert platform.leases[SLOT1_SERIAL] == platform_username_for_rental(rental_a)

    # B with a rental row of their own that *claims* bay 1 while the bay-keyed
    # tenant authority says bay 1 belongs to A -> 403, platform untouched.
    fake_rental_b = str(uuid.uuid4())
    tenant.slots[7] = {"id": fake_rental_b, "rental_id": fake_rental_b, "user_id": CUSTOMER_B, "motherboard_slot_num": 1}
    result = service.create_remote_access(CUSTOMER_B, None, fake_rental_b)
    assert result.http_status == 403
    assert len(platform.calls) == before


# ---------------------------------------------------------------------------
# D. Customer A cannot request another customer's device id
# ---------------------------------------------------------------------------


def test_d_browser_supplied_device_or_slot_is_ignored(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform(registered={SLOT1_SERIAL, SLOT2_SERIAL})
    service, _, _ = _service(tmp_path, tenant=tenant, platform=platform, allowed=(1, 2))
    rental_a = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    rental_b = _rental(tenant, bay=2, user_id=CUSTOMER_B)

    # A asks for slot 2 (B's device) using A's own rental -> rental/slot mismatch -> 403
    assert service.create_remote_access(CUSTOMER_A, 2, rental_a).http_status == 403
    # A asks using B's rental id -> not owned -> 403
    assert service.create_remote_access(CUSTOMER_A, None, rental_b).http_status == 403
    assert SLOT2_SERIAL not in platform.leases
    # Device status for A is always resolved from A's rental, never from the request
    assert service.create_remote_access(CUSTOMER_A, None, rental_a).http_status == 201
    status = service.device_status_for_customer(CUSTOMER_A, rental_a)
    assert status.http_status == 200 and status.body["slot_id"] == 1
    assert platform.calls[-1] == ("status", {"device_id": SLOT1_SERIAL, "workspace_id": "ws-poc"})


def test_d_http_body_device_id_is_ignored(tmp_path: Path):
    server, port, tenant, platform, _ = _start_http(tmp_path)
    base = f"http://127.0.0.1:{port}"
    try:
        token_a, user_a = _signup(base, "a@example.com")
        rental_a = _rental(tenant, bay=1, user_id=user_a)
        status, body, _ = _http(
            "POST",
            f"{base}/rentals/{rental_a}/remote-access",
            token=token_a,
            body={"device_id": SLOT2_SERIAL, "slot_id": 2, "serial": SLOT2_SERIAL},
        )
        assert status == 201, body
        assert body["slot_id"] == 1
        assert platform.leases == {SLOT1_SERIAL: platform_username_for_rental(rental_a)}
        status, body, _ = _http("GET", f"{base}/rentals/{uuid.uuid4()}/remote-access", token=token_a)
        assert status == 404
        assert body["error"] == "rental_not_found"
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# E. Expired rental automatically denies access
# ---------------------------------------------------------------------------


def test_e_expired_rental_denies_access(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform()
    service, store, clock = _service(tmp_path, tenant=tenant, platform=platform)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A, ends_at=clock.now + 20 * 60)

    created = service.create_remote_access(CUSTOMER_A, None, rental)
    assert created.http_status == 201
    # TTL capped by the rental end (20 min), not the configured 60
    assert platform.calls[-1][1]["ttl"] == 20

    clock.now += 21 * 60
    denied = service.get_remote_access(CUSTOMER_A, None, rental)
    assert denied.http_status == 403
    assert service.device_status_for_customer(CUSTOMER_A, rental).http_status == 403
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 403


def test_e_expired_session_denies_device_routes(tmp_path: Path):
    tenant = MemoryTenant()
    service, store, clock = _service(tmp_path, tenant=tenant, platform=FakePlatform(), ttl_minutes=10)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    clock.now += 11 * 60
    got = service.get_remote_access(CUSTOMER_A, None, rental)
    assert got.http_status == 200 and got.body["active"] is False and got.body["status"] == "expired"
    status = service.device_status_for_customer(CUSTOMER_A, rental)
    assert status.http_status == 200
    assert status.body["session_active"] is False
    assert status.body["remote_access_busy"] is False
    reboot = service.reboot_for_customer(CUSTOMER_A, rental)
    assert reboot.http_status == 403
    assert reboot.body["error"] == "session_expired"


# ---------------------------------------------------------------------------
# F. Releasing a rental revokes remote access
# ---------------------------------------------------------------------------


def test_f_release_revokes_platform_access(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform()
    service, store, clock = _service(tmp_path, tenant=tenant, platform=platform)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    created = service.create_remote_access(CUSTOMER_A, None, rental)
    username = platform_username_for_rental(rental)
    assert username in platform.users

    released = service.release_device(1, rental)
    assert released.http_status == 200 and released.body["status"] == "released"
    assert username not in platform.users
    assert SLOT1_SERIAL not in platform.leases
    assert ("release", {"device_id": SLOT1_SERIAL}) in platform.calls
    assert store.get(rental).status == "released"
    after = service.device_status_for_customer(CUSTOMER_A, rental)
    assert after.http_status == 200
    assert after.body["session_active"] is False
    assert after.body["remote_access_busy"] is False


def test_f_customer_revoke(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform()
    service, store, _ = _service(tmp_path, tenant=tenant, platform=platform)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    service.create_remote_access(CUSTOMER_A, None, rental)
    revoked = service.revoke_remote_access(CUSTOMER_A, None, rental)
    assert revoked.http_status == 200 and revoked.body["platform_revoked"] is True
    assert store.get(rental).status == "revoked"
    assert not platform.leases


# ---------------------------------------------------------------------------
# G. Device release makes Slot 1 available again
# ---------------------------------------------------------------------------


def test_g_release_makes_slot1_available_for_next_rental(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=platform)
    rental_a = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental_a).http_status == 201
    assert service.get_device_status(1).body["state"] == "online"

    assert service.release_device(1, rental_a).http_status == 200
    assert service.get_device_status(1).body["state"] == "online"

    # Slot 1 re-assigned to customer B by the existing rental system
    rental_b = str(uuid.uuid4())
    tenant.slots[1] = {"id": rental_b, "rental_id": rental_b, "user_id": CUSTOMER_B, "motherboard_slot_num": 1}
    created = service.create_remote_access(CUSTOMER_B, None, rental_b)
    assert created.http_status == 201
    assert platform.leases[SLOT1_SERIAL] == platform_username_for_rental(rental_b)
    # ...and A (old rental) is now locked out
    assert service.get_remote_access(CUSTOMER_A, None, rental_a).http_status == 404


# ---------------------------------------------------------------------------
# H. Reboot only operates on authorized Slot 1
# ---------------------------------------------------------------------------


def test_h_reboot_only_slot1_via_existing_farm_task(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform()
    farm = FakeFarm()
    status_seq = FarmStatusSequence([[1], [1], [], []])
    service, store, clock = _service(tmp_path, tenant=tenant, platform=platform, farm=farm, farm_status=status_seq)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)

    # Not allowlisted / not mapped slots are refused before any farm call
    assert service.reboot_device(2).http_status == 503
    assert service.reboot_device(19).http_status == 503
    assert farm.tasks == []

    result = service.reboot_device(1)
    assert result.http_status == 200, result.body
    assert result.body["adb_online"] is True and result.body["platform_online"] is True
    assert farm.tasks == [{"type": "reboot", "slot": 1, "payload": {}, "job_id": farm.tasks[0]["job_id"]}]

    # Customer path requires an active session, then runs the same flow
    assert service.reboot_for_customer(CUSTOMER_A, rental).http_status == 409
    service.create_remote_access(CUSTOMER_A, None, rental)
    status_seq._seq = [[1], [], []]
    accepted = service.reboot_for_customer(CUSTOMER_A, rental)
    assert accepted.http_status == 202
    assert store.get(rental).prepare_state == PREPARE_READY
    assert all(task["slot"] == 1 for task in farm.tasks)
    assert [t["type"] for t in farm.tasks if t["type"] != "setup_session_inspect"] == ["reboot", "reboot"]


def test_h_reboot_timeout_is_reported(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform()
    platform.online[SLOT1_SERIAL] = False
    farm = FakeFarm()
    service, store, clock = _service(
        tmp_path, tenant=tenant, platform=platform, farm=farm, farm_status=FarmStatusSequence([[1], [1]])
    )
    result = service.reboot_device(1)
    assert result.http_status == 504 and result.body["error"] == "timeout"
    assert result.body["adb_online"] is False


# ---------------------------------------------------------------------------
# I. Remote-access API credentials never appear in browser responses
# ---------------------------------------------------------------------------


def test_i_admin_credentials_and_serial_never_leak(tmp_path: Path):
    server, port, tenant, platform, _ = _start_http(tmp_path)
    base = f"http://127.0.0.1:{port}"
    admin_markers = ("gads-admin-user", "gads-admin-password", "admin-jwt", SLOT1_SERIAL)
    try:
        token_a, user_a = _signup(base, "a@example.com")
        rental = _rental(tenant, bay=1, user_id=user_a)
        responses = []
        responses.append(_http("POST", f"{base}/rentals/{rental}/remote-access", token=token_a, body={}))
        responses.append(_http("GET", f"{base}/rentals/{rental}/remote-access", token=token_a))
        responses.append(_http("POST", f"{base}/rentals/{rental}/remote-access/device-status", token=token_a, body={}))
        responses.append(_http("POST", f"{base}/rentals/{rental}/remote-access/revoke", token=token_a, body={}))
        responses.append(_http("GET", f"{base}/rentals/{rental}/remote-access", token=token_a))
        for status, body, headers in responses:
            raw = json.dumps(body) + json.dumps(headers)
            for marker in admin_markers:
                assert marker not in raw, (status, marker)
        created = responses[0][1]
        assert created["session_mode"] == "in_app"
        assert "platform_login" not in created
        assert "password" not in json.dumps(created)
        assert created["stream_path"].endswith("/remote-access/stream")
        # Subsequent GET does not replay credentials
        assert "platform_login" not in responses[1][1]
        assert responses[1][1]["activation_state"] == "REMOTE_ACCESS_READY"
    finally:
        server.shutdown()


def test_i_gads_client_uses_admin_token_server_side_only():
    seen: list[dict] = []

    class Session:
        def request(self, method, url, json=None, params=None, headers=None, timeout=None):
            seen.append({"method": method, "url": url, "json": json, "headers": headers or {}})
            if url.endswith("/authenticate"):
                who = (json or {}).get("username")
                return _Resp(200, {"success": True, "result": {"access_token": f"jwt-{who}"}})
            if url.endswith("/admin/devices"):
                return _Resp(200, {"success": True, "result": {"devices": [{"udid": SLOT1_SERIAL, "workspace_id": "ws"}]}})
            if url.endswith("/admin/user") and method == "POST":
                return _Resp(200, {"success": True})
            if "/admin/user/" in url and method == "DELETE":
                return _Resp(404, {"success": False})
            raise AssertionError(url)

    client = GadsHubClient("http://hub", admin_username="gads-admin-user", admin_password="gads-admin-password", session=Session())  # type: ignore[arg-type]
    platform = GadsRemoteAccessPlatform(client, workspace_id="ws", public_url="https://remote.example")
    grant = platform.grant_access(device_id=SLOT1_SERIAL, rental_id="abcd1234-0000", ttl_minutes=30)
    assert grant.platform_username == "rental-abcd12340000"
    assert grant.platform_password and grant.platform_password != "gads-admin-password"
    assert grant.expires_at > 0
    assert [s for s in seen if s["url"].endswith("/lock") or "/unlock" in s["url"]] == []
    add_user = [s for s in seen if s["url"].endswith("/admin/user") and s["method"] == "POST"][0]
    assert add_user["headers"]["Authorization"] == "Bearer jwt-gads-admin-user"
    assert add_user["json"]["role"] == "user" and add_user["json"]["workspace_ids"] == ["ws"]
    auths = [s for s in seen if s["url"].endswith("/authenticate")]
    assert auths[-1]["json"]["username"] == grant.platform_username


def test_i_gads_grant_refuses_http_public_url():
    class Session:
        def request(self, method, url, json=None, params=None, headers=None, timeout=None):
            raise AssertionError("must not call GADS when the customer URL is not HTTPS")

    client = GadsHubClient("http://hub.local:10000", admin_username="a", admin_password="b", session=Session())  # type: ignore[arg-type]
    platform = GadsRemoteAccessPlatform(client, workspace_id="ws-poc", public_url="http://hub.local:10000")
    with pytest.raises(RemoteAccessPlatformError, match="public_url_not_https"):
        platform.grant_access(device_id=SLOT1_SERIAL, rental_id="r", ttl_minutes=10)


def test_create_fails_closed_when_public_url_not_https(tmp_path: Path):
    class HttpPublicPlatform(FakePlatform):
        def grant_access(self, *, device_id: str, rental_id: str, ttl_minutes: int, workspace_id: str = "") -> PlatformAccessGrant:
            raise RemoteAccessPlatformError("public_url_not_https")

    tenant = MemoryTenant()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=HttpPublicPlatform())
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    result = service.create_remote_access(CUSTOMER_A, None, rental)
    assert result.http_status == 503
    assert result.body["error"] == "gads_unavailable"
    assert "100.118" not in json.dumps(result.body)
    assert "password" not in json.dumps(result.body).lower()


def test_gads_platform_from_config_does_not_fall_back_to_http_hub():
    from types import SimpleNamespace

    from infrastructure.gads_remote_access import gads_platform_from_config

    config = SimpleNamespace(
        remote_access_platform_url="http://127.0.0.1:10000",
        remote_access_public_url=None,
        remote_access_admin_username="admin",
        remote_access_admin_password="secret",
        remote_access_workspace_id="ws",
        request_timeout_seconds=10.0,
    )
    platform = gads_platform_from_config(config)
    assert platform is not None
    with pytest.raises(RemoteAccessPlatformError, match="public_url_not_https"):
        platform.grant_access(device_id=SLOT1_SERIAL, rental_id="r", ttl_minutes=10)


def test_i_gads_grant_refuses_device_outside_poc_workspace():
    class Session:
        def request(self, method, url, json=None, params=None, headers=None, timeout=None):
            if url.endswith("/authenticate"):
                return _Resp(200, {"success": True, "result": {"access_token": "t"}})
            if url.endswith("/admin/devices"):
                return _Resp(200, {"success": True, "result": {"devices": [{"udid": SLOT1_SERIAL, "workspace_id": "default"}]}})
            raise AssertionError(url)

    client = GadsHubClient("http://hub", admin_username="a", admin_password="b", session=Session())  # type: ignore[arg-type]
    platform = GadsRemoteAccessPlatform(client, workspace_id="ws-poc", public_url="https://remote.example")
    with pytest.raises(RemoteAccessPlatformError, match="device_not_in_poc_workspace"):
        platform.grant_access(device_id=SLOT1_SERIAL, rental_id="r", ttl_minutes=10)


# ---------------------------------------------------------------------------
# J. POC remains disabled for all slots other than Slot 1
# ---------------------------------------------------------------------------


def test_j_default_config_is_disabled_and_slot1_only(monkeypatch, tmp_path: Path):
    for key in list(__import__("os").environ):
        if key.startswith("REMOTE_ACCESS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    monkeypatch.setenv("HARDWARE_AGENT_TOKEN", "tok")
    config = load_config(env_file=None)
    assert config.remote_access_poc_enabled is False
    assert config.remote_access_poc_slot_ids == (1,)
    assert config.remote_access_slot_ids is None
    assert config.remote_access_prepare_slot_ids is None
    assert config.remote_access_observe_slot_ids is None
    assert config.remote_access_platform_url is None
    monkeypatch.setenv("REMOTE_ACCESS_POC_SLOT_IDS", "1,3")
    monkeypatch.setenv("REMOTE_ACCESS_SESSION_TTL_MINUTES", "500")
    with pytest.raises(Exception):
        load_config(env_file=None)  # TTL above the GADS 360-minute cap is rejected


def test_j_disabled_service_refuses_everything(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform()
    farm = FakeFarm()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=farm, enabled=False)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 503
    assert service.get_device_status(1).http_status == 503
    assert service.reboot_device(1).http_status == 503
    assert service.release_device(1, rental).http_status == 503
    assert platform.calls == [] and farm.tasks == []


def test_j_other_slots_refused_even_with_valid_rental(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform(registered={SLOT1_SERIAL, SLOT2_SERIAL})
    farm = FakeFarm()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=farm)
    rental_b = _rental(tenant, bay=2, user_id=CUSTOMER_B)
    assert service.create_remote_access(CUSTOMER_B, None, rental_b).http_status == 503
    assert service.get_device_status(2).http_status == 503
    assert service.reboot_device(2).http_status == 503
    assert service.release_device(2, rental_b).http_status == 503
    assert platform.calls == [] and farm.tasks == []


def test_j_farm_qr_task_gated_by_flag_and_allowlist():
    config_off = _agent_config(remote_access_poc_enabled=False)
    config_on = _agent_config(remote_access_poc_enabled=True, remote_access_poc_slot_ids=(1,))
    runner = _QrRunner()
    downloader = lambda url, timeout: PNG_BYTES  # noqa: E731
    slot_map = {1: SLOT1_SERIAL, 2: SLOT2_SERIAL}
    req = lambda slot: FarmTaskRequest(  # noqa: E731
        job_id="job-1", task_type=REMOTE_ACCESS_PLACE_QR_TASK, farm_slot_id=slot,
        payload={"esim_qr_url": "https://example.test/qr.png", "rental_id": "r1"},
    )
    off = run_remote_access_place_qr(adb_path="adb", slot_map=slot_map, request=req(1), agent_config=config_off, command_runner=runner, downloader=downloader)
    assert off.http_status == 403 and off.error == "remote_access_poc_disabled"
    mapped_two = run_remote_access_place_qr(adb_path="adb", slot_map=slot_map, request=req(2), agent_config=config_on, command_runner=runner, downloader=downloader)
    assert mapped_two.ok is True
    assert mapped_two.error != "slot_not_allowlisted"
    assert mapped_two.details["serial"] == SLOT2_SERIAL
    assert {serial for serial, _ in runner.calls} == {SLOT2_SERIAL}
    runner.calls.clear()
    # Executor dispatch is also wired (without config -> not configured)
    via_executor = execute_farm_task(adb_path="adb", slot_map=slot_map, request=req(1), agent_config=None, deps=FarmTaskExecutorDeps(command_runner=runner))
    assert via_executor.http_status == 503
    assert runner.calls == []


# ---------------------------------------------------------------------------
# QR placement task (Slot 1): DCIM push, no provisioning
# ---------------------------------------------------------------------------


class _QrRunner(AdbCommandRunner):
    def __init__(self, state: str = "device", *, ls_stdout: str | None = None, remote_size: int | None = 1024) -> None:
        super().__init__()
        self.state = state
        self.calls: list[tuple[str, list[str]]] = []
        self.ls_stdout = ls_stdout
        self.remote_size = remote_size

    def run(self, serial: str, arguments: list[str]) -> AdbCommandResult:
        self.calls.append((serial, list(arguments)))
        if arguments == ["get-state"]:
            return AdbCommandResult(stdout=self.state, stderr="")
        if arguments[:2] == ["shell", "ls"] and arguments[2:]:
            if self.ls_stdout is not None:
                return AdbCommandResult(stdout=self.ls_stdout, stderr="")
            return AdbCommandResult(stdout=arguments[-1], stderr="")
        if arguments[:3] == ["shell", "wc", "-c"]:
            path = arguments[-1]
            size = 0 if self.remote_size is None else int(self.remote_size)
            return AdbCommandResult(stdout=f"{size} {path}", stderr="")
        if arguments[:4] == ["shell", "stat", "-c", "%s"]:
            size = 0 if self.remote_size is None else int(self.remote_size)
            return AdbCommandResult(stdout=str(size), stderr="")
        return AdbCommandResult(stdout="", stderr="")


def _agent_config(**overrides) -> AgentConfig:
    base = dict(
        supabase_url="https://example.supabase.co",
        supabase_anon_key="anon",
        supabase_table="hardware_queue",
        hardware_agent_token="tok",
        heartbeat_interval_seconds=15.0,
        request_timeout_seconds=10.0,
        log_level="INFO",
        adb_path="adb",
        provisioning_allowed_slot_ids=(1,),
    )
    base.update(overrides)
    return AgentConfig(**base)


def test_qr_task_pushes_image_to_dcim_camera_only():
    runner = _QrRunner()
    config = _agent_config(remote_access_poc_enabled=True)
    req = FarmTaskRequest(
        job_id="11111111-2222-3333-4444-555555555555",
        task_type=REMOTE_ACCESS_PLACE_QR_TASK,
        farm_slot_id=1,
        payload={"esim_qr_url": "https://example.test/qr.png", "rental_id": "aaaaaaaa-bbbb"},
    )
    result = run_remote_access_place_qr(
        adb_path="adb", slot_map={1: SLOT1_SERIAL}, request=req, agent_config=config,
        command_runner=runner, downloader=lambda url, timeout: PNG_BYTES,
    )
    assert result.ok, result
    serials = {serial for serial, _ in runner.calls}
    assert serials == {SLOT1_SERIAL}
    push = [args for _, args in runner.calls if args and args[0] == "push"][0]
    assert push[2] == "/sdcard/DCIM/Camera/mobirent_esim_qr_aaaaaaaabbbb_11111111.png"
    joined = " ".join(" ".join(args) for _, args in runner.calls)
    for forbidden in ("provision", "euicc", "dpm", "pm grant", "WRITE_EMBEDDED", "forward", "localabstract"):
        assert forbidden not in joined
    assert "media_scanned=true" in (result.message or "")
    details = result.details or {}
    assert details["ok"] is True
    assert details["placed"] is True
    assert details["serial"] == SLOT1_SERIAL
    assert details["destination"] == push[2]
    assert details["downloaded_size"] == len(PNG_BYTES)
    assert details["remote_size"] == 1024
    assert details["error_code"] is None
    assert any(args[:3] == ["shell", "wc", "-c"] for _, args in runner.calls)


def test_qr_task_rejects_non_image_and_private_urls():
    runner = _QrRunner()
    config = _agent_config(remote_access_poc_enabled=True)
    base = dict(job_id="j", task_type=REMOTE_ACCESS_PLACE_QR_TASK, farm_slot_id=1)
    bad_image = run_remote_access_place_qr(
        adb_path="adb", slot_map={1: SLOT1_SERIAL}, agent_config=config, command_runner=runner,
        request=FarmTaskRequest(**base, payload={"esim_qr_url": "https://example.test/qr.png"}),
        downloader=lambda url, timeout: b"LPA:1$smdp.example$CODE",
    )
    assert bad_image.error == "qr_not_an_image" and runner.calls == []
    private = run_remote_access_place_qr(
        adb_path="adb", slot_map={1: SLOT1_SERIAL}, agent_config=config, command_runner=runner,
        request=FarmTaskRequest(**base, payload={"esim_qr_url": "https://127.0.0.1/qr.png"}),
        downloader=lambda url, timeout: PNG_BYTES,
    )
    assert private.error == "invalid_assignment" and runner.calls == []
    with pytest.raises(ValueError, match="not allowed"):
        parse_farm_task_body({"job_id": "j", "type": REMOTE_ACCESS_PLACE_QR_TASK, "farm_slot_id": 1, "payload": {"serial": "x"}})


def test_prepare_esim_flow_places_qr_then_reboots(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform()
    farm = FakeFarm()
    service, store, clock = _service(
        tmp_path, tenant=tenant, platform=platform, farm=farm, farm_status=FarmStatusSequence([[1], [], []])
    )
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.prepare_esim(CUSTOMER_A, rental).http_status == 409  # no active session yet
    service.create_remote_access(CUSTOMER_A, None, rental)
    accepted = service.prepare_esim(CUSTOMER_A, rental)
    assert accepted.http_status == 202, accepted.body
    action_tasks = [t for t in farm.tasks if t["type"] != "setup_session_inspect"]
    assert [t["type"] for t in action_tasks] == [REMOTE_ACCESS_PLACE_QR_TASK, "reboot"]
    assert action_tasks[0]["slot"] == 1
    assert action_tasks[0]["payload"]["esim_qr_url"] == "https://example.test/private/qr"
    assert "provision" not in json.dumps(farm.tasks)
    assert store.get(rental).prepare_state == PREPARE_READY


def test_prepare_esim_fails_closed_when_qr_ref_not_allowlisted(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    service, store, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A, qr_code_url="https://evil.example/qr.png")
    service.create_remote_access(CUSTOMER_A, None, rental)
    result = service.prepare_esim(CUSTOMER_A, rental)
    assert result.http_status == 400
    assert all(t["type"] == "setup_session_inspect" for t in farm.tasks)


def test_prepare_esim_records_failure_when_farm_rejects(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    farm.fail_types.add(REMOTE_ACCESS_PLACE_QR_TASK)
    service, store, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    service.create_remote_access(CUSTOMER_A, None, rental)
    assert service.prepare_esim(CUSTOMER_A, rental).http_status == 202
    session = store.get(rental)
    assert session.prepare_state == PREPARE_FAILED and session.prepare_detail.startswith("place_qr:")
    assert [t["type"] for t in farm.tasks if t["type"] != "setup_session_inspect"] == [REMOTE_ACCESS_PLACE_QR_TASK]  # no reboot after failure


# ---------------------------------------------------------------------------
# K. Existing production provisioning code still behaves unchanged
# ---------------------------------------------------------------------------


def test_k_existing_task_types_and_routes_unchanged():
    from application.farm_task_types import SUPPORTED_TASK_TYPES

    assert {"reboot", "assign", "airplane_cycle", "voidfix_repair"} <= SUPPORTED_TASK_TYPES
    # Existing routes still parse to the same kinds
    slot = str(uuid.uuid4())
    assert parse_route(f"/slots/{slot}/esim").kind == "slot_esim"
    assert parse_route(f"/slots/{slot}/actions/reboot").kind == "slot_action"
    assert parse_route("/farm/slots/1/assign").kind == "farm_assign"
    assert parse_route(f"/rentals/{slot}/remote-access").kind == "remote_access"
    assert parse_route(f"/rentals/{slot}/remote-access/prepare-esim").action == "prepare-esim"
    assert parse_route(f"/rentals/{slot}/esim/upload").kind == "esim_qr_upload"
    assert parse_route(f"/rentals/{slot}/esim/upload").rental_id == slot
    assert parse_route(f"/rentals/{slot}/end").kind == "rental_end"
    assert parse_route(f"/rentals/{slot}/end").rental_id == slot
    assert parse_route(f"/rentals/{slot}/remote-access/control").action == "control"
    assert parse_route(f"/rentals/{slot}/remote-access/reconnect-cellular").action == "reconnect-cellular"
    assert parse_route(f"/rentals/{slot}/remote-access/troubleshoot").action == "troubleshoot"
    assert parse_route(f"/rentals/{slot}/remote-access/stream").action == "stream"
    assert parse_route(f"/rentals/{slot}/cancel").kind == "rental_cancel"
    assert parse_route("/farm/slots/1/cleanup-verified").kind == "cleanup_verified"
    assert parse_route(f"/rentals/{slot}/remote-access/adb-shell") is None
    assert parse_route(f"/rentals/not-a-uuid/remote-access") is None


def test_k_assign_task_unaffected_by_poc_flags():
    """`assign` keeps its own allowlist regardless of REMOTE_ACCESS_* settings."""
    from domain.models import ActivationJob, ProvisioningResult
    from domain.provisioning_state import ActivationVerdict
    from domain.slot_isolation import SlotIsolationPolicy

    class Provisioner:
        def __init__(self) -> None:
            self.calls = []

        def provision(self, serial: str, job: ActivationJob) -> ProvisioningResult:
            self.calls.append(serial)
            return ProvisioningResult(
                success=True,
                job_id=job.job_id,
                slot_id=job.slot_id,
                verdict=ActivationVerdict.ACTIVATION_CONFIRMED,
            )

    class Resolver:
        def resolve(self, job: ActivationJob) -> ActivationJob:
            return job.with_activation_code("LPA:1$smdp.example$CODE")

    for poc_enabled in (False, True):
        config = _agent_config(remote_access_poc_enabled=poc_enabled, remote_access_poc_slot_ids=(1,), provisioning_allowed_slot_ids=(1, 2))
        provisioner = Provisioner()
        runner = _QrRunner()
        result = execute_farm_task(
            adb_path="adb",
            slot_map={1: SLOT1_SERIAL, 2: SLOT2_SERIAL},
            request=FarmTaskRequest(job_id=str(uuid.uuid4()), task_type="assign", farm_slot_id=2, payload={"esim_qr_url": "https://example.test/qr.png"}),
            agent_config=config,
            deps=FarmTaskExecutorDeps(command_runner=runner, provisioner=provisioner, payload_resolver=Resolver(), isolation=SlotIsolationPolicy((1, 2))),
        )
        assert result.ok is False, (poc_enabled, result)
        assert "human Settings/LPA required" in (result.message or "")
        assert provisioner.calls == []
        denied = execute_farm_task(
            adb_path="adb",
            slot_map={1: SLOT1_SERIAL, 2: SLOT2_SERIAL},
            request=FarmTaskRequest(job_id=str(uuid.uuid4()), task_type="assign", farm_slot_id=2, payload={"esim_qr_url": "https://example.test/qr.png"}),
            agent_config=config,
            deps=FarmTaskExecutorDeps(command_runner=runner, provisioner=provisioner, payload_resolver=Resolver(), isolation=SlotIsolationPolicy((1,))),
        )
        assert denied.ok is False
        assert denied.error == "provisioning_failed"
        assert provisioner.calls == []


def test_k_vps_server_without_poc_flag_has_no_remote_access_routes(tmp_path: Path):
    server, port, tenant, _platform, handler = _start_http(tmp_path, with_service=False)
    base = f"http://127.0.0.1:{port}"
    try:
        token_a, user_a = _signup(base, "a@example.com")
        rental = _rental(tenant, bay=1, user_id=user_a)
        status, body, _ = _http("POST", f"{base}/rentals/{rental}/remote-access", token=token_a, body={})
        assert status == 503 and body["error"] == "phone_unavailable"
        # Existing user route still works
        status, listed, _ = _http("GET", f"{base}/slots", token=token_a)
        assert status == 200 and listed["count"] == 1
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# HTTP harness (VPS handler with the POC service attached)
# ---------------------------------------------------------------------------


def _load_mod():
    path = ROOT / "tools" / "vps_backend_server.py"
    spec = importlib.util.spec_from_file_location("vps_backend_server_remote_access", path)
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
            return resp.status, json.loads(resp.read().decode()), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        return exc.code, (json.loads(raw) if raw else {}), dict(exc.headers)


def _signup(base: str, email: str) -> tuple[str, str]:
    status, body, _ = _http("POST", f"{base}/auth/signup", body={"email": email, "password": STRONG})
    assert status == 200, body
    return body["session"]["access_token"], body["user"]["id"]


def _start_http(tmp_path: Path, *, with_service: bool = True, slot_msisdn_map_path: str | None = None):
    from application.vps_farm_management_service import VpsFarmManagementService
    from application.vps_job_worker import VpsJobWorker
    from infrastructure.auth_rate_limiter import AuthRateLimiter
    from infrastructure.slot_assignment_store import SlotAssignmentStore
    from infrastructure.slot_event_store import SlotEventStore
    from infrastructure.slot_status_store import SlotStatusStore
    from infrastructure.vps_job_store import VpsJobStore
    from infrastructure.vps_rate_limiter import VpsRateLimiter

    mod = _load_mod()
    Handler = mod.Handler
    gotrue = MemoryGoTrue()
    tenant = MemoryTenant()
    platform = FakePlatform()

    class SyncWorker(VpsJobWorker):
        def enqueue_process(self, job_id: str) -> None:
            return None

    jobs = VpsJobStore(tmp_path / "jobs.sqlite")
    assign = SlotAssignmentStore(tmp_path / "assign.sqlite")
    events = SlotEventStore(tmp_path / "events.sqlite")
    status = SlotStatusStore(tmp_path / "status.sqlite")
    worker = SyncWorker(job_store=jobs, assignment_store=assign, event_store=events, farm_task_client=None, poll_interval_seconds=3600.0, auth_store=tenant)
    farm_svc = VpsFarmManagementService(
        job_store=jobs, assignment_store=assign, event_store=events, job_worker=worker,
        farm_status_fetcher=lambda: {"ok": True, "offline_slots": [], "mapped_slots": [1, 2], "slot_count": 2, "adb_online": 2},
        known_farm_slots={1, 2}, rate_limiter=VpsRateLimiter(per_slot_limit=100, global_limit=1000),
        status_store=status, auth_store=tenant, esim_url_prefixes=("https://example.test/",),
    )
    service = None
    if with_service:
        service, _, _ = _service(
            tmp_path,
            tenant=tenant,
            platform=platform,
            farm=FakeFarm(),
            slot_msisdn_map_path=slot_msisdn_map_path,
        )
        farm_svc.set_remote_access(service)
    Handler.farm_service_token = FARM_TOKEN
    Handler.auth_service = AuthService(supabase=gotrue, tenant=tenant)
    Handler.auth_rate_limiter = AuthRateLimiter(limit=100, window_seconds=60.0, max_keys=64)
    Handler.farm_management_service = farm_svc
    Handler.slot_sms_service = None
    Handler.allowed_origins = frozenset()
    Handler.auth_cookie_name = None
    Handler.webhook_secret = None
    Handler.tenant_store = tenant
    Handler.remote_access_service = service
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1], tenant, platform, Handler


def test_http_full_customer_flow_and_isolation(tmp_path: Path):
    server, port, tenant, platform, _ = _start_http(tmp_path)
    base = f"http://127.0.0.1:{port}"
    try:
        token_a, user_a = _signup(base, "a@example.com")
        token_b, user_b = _signup(base, "b@example.com")
        rental_a = _rental(tenant, bay=1, user_id=user_a)

        # unauthenticated -> 401
        status, _, _ = _http("POST", f"{base}/rentals/{rental_a}/remote-access", body={})
        assert status == 401
        # farm-service token cannot create customer access
        status, _, _ = _http("POST", f"{base}/rentals/{rental_a}/remote-access", token=FARM_TOKEN, body={})
        assert status == 401
        # B on A's rental -> 403
        status, body, _ = _http("POST", f"{base}/rentals/{rental_a}/remote-access", token=token_b, body={})
        assert status == 403 and body["error"] == "rental_not_owned"
        # A creates
        status, created, _ = _http("POST", f"{base}/rentals/{rental_a}/remote-access", token=token_a, body={})
        assert status == 201 and created["session_mode"] == "in_app"
        assert "platform_login" not in created
        # A device status ok; B 403
        status, ds, _ = _http("POST", f"{base}/rentals/{rental_a}/remote-access/device-status", token=token_a, body={})
        assert status == 200 and ds["state"] == "online" and ds["slot_id"] == 1
        status, _, _ = _http("POST", f"{base}/rentals/{rental_a}/remote-access/device-status", token=token_b, body={})
        assert status == 403
        # Lovable farm-service releases at end of rental
        status, rel, _ = _http("POST", f"{base}/rentals/{rental_a}/remote-access/release", token=FARM_TOKEN, body={})
        assert status == 200 and rel["status"] == "released"
        assert not platform.leases
        status, after, _ = _http("GET", f"{base}/rentals/{rental_a}/remote-access", token=token_a)
        assert status == 200 and after["active"] is False and after["status"] == "released"
        # prepare-esim ignores a spoof QR URL in the body (rental already released → 403)
        status, body, _ = _http(
            "POST",
            f"{base}/rentals/{rental_a}/remote-access/prepare-esim",
            token=token_a,
            body={"esim_qr_url": "https://evil.example/qr.png", "slot_id": 2, "device_id": SLOT2_SERIAL},
        )
        assert status == 403 and body["error"] == "session_expired"
    finally:
        server.shutdown()


def test_existing_valid_session_is_reused_without_new_grant(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=platform)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    first = service.create_remote_access(CUSTOMER_A, None, rental)
    assert first.http_status == 201
    grants = [c for c in platform.calls if c[0] == "grant"]
    assert len(grants) == 1
    reused = service.create_remote_access(CUSTOMER_A, None, rental)
    assert reused.http_status == 200
    assert reused.body["ok"] is True
    assert reused.body.get("active") is True or reused.body.get("status") == STATUS_ACTIVE
    assert [c for c in platform.calls if c[0] == "grant"] == grants
    assert [c for c in platform.calls if c[0] == "revoke"] == []
    assert platform.leases[SLOT1_SERIAL] == platform_username_for_rental(rental)


def test_prepare_esim_is_idempotent_while_ready(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    service, store, _ = _service(
        tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm, farm_status=FarmStatusSequence([[1], [], []])
    )
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    service.create_remote_access(CUSTOMER_A, None, rental)
    first = service.prepare_esim(CUSTOMER_A, rental)
    assert first.http_status == 202
    second = service.prepare_esim(CUSTOMER_A, rental)
    assert second.http_status == 202
    assert second.body["prepare_state"] == PREPARE_READY
    assert [t["type"] for t in farm.tasks if t["type"] != "setup_session_inspect"] == [REMOTE_ACCESS_PLACE_QR_TASK, "reboot"]


def test_prepare_esim_storage_key_without_https_url_fails_closed(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A, qr_code_url="users/abc/esim.png")
    service.create_remote_access(CUSTOMER_A, None, rental)
    result = service.prepare_esim(CUSTOMER_A, rental)
    assert result.http_status == 503
    assert all(t["type"] == "setup_session_inspect" for t in farm.tasks)


def test_expired_rental_revokes_platform_lease(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform()
    service, _, clock = _service(tmp_path, tenant=tenant, platform=platform)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A, ends_at=clock.now + 20 * 60)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    clock.now += 21 * 60
    assert service.get_remote_access(CUSTOMER_A, None, rental).http_status == 403
    assert SLOT1_SERIAL not in platform.leases
    assert platform_username_for_rental(rental) not in platform.users


def test_sweep_releases_when_owner_changes(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=platform)
    rental_a = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental_a).http_status == 201
    tenant.slots[1] = {"id": rental_a, "rental_id": rental_a, "user_id": CUSTOMER_B, "motherboard_slot_num": 1}
    service.sweep_stale_sessions()
    assert SLOT1_SERIAL not in platform.leases


def test_activation_observation_does_not_fake_success(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    farm.activation_details = {
        "verdict": "ACTIVATION_FAILED",
        "esim_profile_present": False,
        "esim_enabled": True,
        "network_registered": False,
        "cellular": False,
        "observation_complete": True,
    }
    service, store, _ = _service(
        tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm, farm_status=FarmStatusSequence([[1], [], []])
    )
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    created = service.create_remote_access(CUSTOMER_A, None, rental)
    assert created.body["activation_state"] == "REMOTE_ACCESS_READY"
    service.prepare_esim(CUSTOMER_A, rental)
    assert store.get(rental).prepare_state == PREPARE_READY
    observed = service.activation_status_for_customer(CUSTOMER_A, rental)
    assert observed.http_status == 200
    assert observed.body["activation_state"] == "CUSTOMER_ACTIVATION_REQUIRED"
    assert observed.body["qr_ready"] is True
    assert "LPA" not in json.dumps(observed.body)
    farm.activation_details = {
        "verdict": "ACTIVATION_CONFIRMED",
        "esim_profile_present": True,
        "esim_enabled": True,
        "network_registered": True,
        "cellular": True,
        "observation_complete": True,
    }
    farm.activation_details = {
        "verdict": "ACTIVATION_PARTIAL",
        "esim_profile_present": True,
        "esim_enabled": False,
        "network_registered": False,
        "cellular": False,
        "observation_complete": True,
    }
    partial = service.activation_status_for_customer(CUSTOMER_A, rental)
    assert partial.body["activation_state"] == "ACTIVATING"
    assert partial.body["qr_ready"] is True
    farm.activation_details = {
        "verdict": "ACTIVATION_CONFIRMED",
        "esim_profile_present": True,
        "esim_enabled": True,
        "network_registered": True,
        "cellular": True,
        "observation_complete": True,
    }
    confirmed = service.activation_status_for_customer(CUSTOMER_A, rental)
    assert confirmed.body["activation_state"] == "ACTIVE"


def test_get_refresh_observes_and_can_return_active(tmp_path: Path):
    """Published Refresh is GET /remote-access; it must persist CONFIRMED like Check activation."""
    tenant = MemoryTenant()
    farm = FakeFarm()
    farm.activation_details = {
        "verdict": "ACTIVATION_CONFIRMED",
        "esim_profile_present": True,
        "esim_enabled": True,
        "network_registered": True,
        "cellular": True,
        "observation_complete": True,
    }
    service, store, _ = _service(
        tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm, farm_status=FarmStatusSequence([[1], [], []])
    )
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    service.prepare_esim(CUSTOMER_A, rental)
    assert store.get(rental).prepare_state == PREPARE_READY
    assert store.get(rental).activation_observed is None
    refreshed = service.get_remote_access(CUSTOMER_A, None, rental)
    assert refreshed.http_status == 200
    assert refreshed.body["activation_state"] == "ACTIVE"
    assert refreshed.body["qr_ready"] is True
    assert refreshed.body["activation_observed"] == "confirmed"
    assert refreshed.body["activation_observed_at"]
    assert refreshed.body["activation_evidence"]["verdict"] == "ACTIVATION_CONFIRMED"
    assert store.get(rental).activation_observed == "confirmed"
    observe_tasks = [t for t in farm.tasks if t["type"] == "remote_access_activation_status"]
    assert observe_tasks


def test_get_refresh_skips_observe_while_rebooting(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    farm.activation_details = {
        "verdict": "ACTIVATION_CONFIRMED",
        "esim_profile_present": True,
        "esim_enabled": True,
        "network_registered": True,
        "cellular": True,
        "observation_complete": True,
    }
    service, store, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    store.set_prepare_state(rental, PREPARE_REBOOTING, detail="in_progress")
    got = service.get_remote_access(CUSTOMER_A, None, rental)
    assert got.body["activation_state"] == "DEVICE_REBOOTING"
    assert store.get(rental).activation_observed is None
    assert not any(t["type"] == "remote_access_activation_status" for t in farm.tasks)
    checked = service.activation_status_for_customer(CUSTOMER_A, rental)
    assert checked.http_status == 200
    assert checked.body["activation_state"] == "DEVICE_REBOOTING"
    assert not any(t["type"] == "remote_access_activation_status" for t in farm.tasks)


def test_activation_status_without_gads_session_is_forbidden(tmp_path: Path):
    """assign-then-activation-status: rental exists, no remote session → 403 (do not weaken)."""
    tenant = MemoryTenant()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=FakeFarm())
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    denied = service.activation_status_for_customer(CUSTOMER_A, rental)
    assert denied.http_status == 409
    assert denied.body["error"] == "remote_access_not_ready"


def test_http_200_without_confirmation_is_not_active(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    service, _, _ = _service(
        tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm, farm_status=FarmStatusSequence([[1], [], []])
    )
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    service.prepare_esim(CUSTOMER_A, rental)
    farm.activation_details = {
        "verdict": "ACTIVATION_FAILED",
        "esim_profile_present": False,
        "esim_enabled": True,
        "network_registered": False,
        "cellular": False,
        "observation_complete": True,
    }
    body = service.activation_status_for_customer(CUSTOMER_A, rental).body
    assert body["ok"] is True
    assert body["activation_state"] != "ACTIVE"
    assert body["activation_observed"] == "missing"


def test_repeated_refresh_does_not_duplicate_farm_observe(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    farm.activation_details = {
        "verdict": "ACTIVATION_CONFIRMED",
        "esim_profile_present": True,
        "esim_enabled": True,
        "network_registered": True,
        "cellular": True,
        "observation_complete": True,
    }
    service, _, _ = _service(
        tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm, farm_status=FarmStatusSequence([[1], [], []])
    )
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    service.prepare_esim(CUSTOMER_A, rental)
    first = service.get_remote_access(CUSTOMER_A, None, rental)
    assert first.body["activation_state"] == "ACTIVE"
    n = len([t for t in farm.tasks if t["type"] == "remote_access_activation_status"])
    second = service.get_remote_access(CUSTOMER_A, None, rental)
    assert second.body["activation_state"] == "ACTIVE"
    assert len([t for t in farm.tasks if t["type"] == "remote_access_activation_status"]) == n


def test_restart_reconciles_in_progress_prepare(tmp_path: Path):
    tenant = MemoryTenant()
    service, store, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform())
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    store.set_prepare_state(rental, PREPARE_PLACING_QR, detail="running")
    assert store.reconcile_interrupted_prepares() == 1
    session = store.get(rental)
    assert session.prepare_state == PREPARE_FAILED
    assert session.prepare_detail == "interrupted_by_restart"
    assert session.activation_observed != "confirmed"


def test_release_is_idempotent(tmp_path: Path):
    tenant = MemoryTenant()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform())
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    first = service.release_device(1, rental)
    second = service.release_device(1, rental)
    assert first.http_status == 200 and second.http_status == 200
    assert second.body["status"] == "released"


def test_http_prepare_esim_ignores_body_qr_url(tmp_path: Path):
    server, port, tenant, platform, _ = _start_http(tmp_path)
    base = f"http://127.0.0.1:{port}"
    try:
        token_a, user_a = _signup(base, "a@example.com")
        rental_a = _rental(tenant, bay=1, user_id=user_a)
        status, _, _ = _http("POST", f"{base}/rentals/{rental_a}/remote-access", token=token_a, body={})
        assert status == 201
        status, body, _ = _http(
            "POST",
            f"{base}/rentals/{rental_a}/remote-access/prepare-esim",
            token=token_a,
            body={"esim_qr_url": "https://evil.example/stolen.png", "serial": SLOT2_SERIAL},
        )
        assert status == 202, body
        farm = server.RequestHandlerClass.remote_access_service._farm
        place = next(t for t in farm.tasks if t["type"] == "remote_access_place_qr")
        assert place["payload"]["esim_qr_url"] == "https://example.test/private/qr"
        assert "evil.example" not in json.dumps(farm.tasks)
        status, denied, _ = _http(
            "POST",
            f"{base}/rentals/{rental_a}/remote-access/activation-status",
            token="not-a-token",
            body={},
        )
        assert status == 401
    finally:
        server.shutdown()


def test_activation_farm_task_allows_mapped_non_poc_slots():
    from application.remote_access_farm_task import run_remote_access_activation_status

    runner = _QrRunner()
    config = _agent_config(remote_access_poc_enabled=True, remote_access_poc_slot_ids=(1,))
    other = run_remote_access_activation_status(
        adb_path="adb",
        slot_map={1: SLOT1_SERIAL, 2: SLOT2_SERIAL},
        request=FarmTaskRequest(job_id="j", task_type="remote_access_activation_status", farm_slot_id=2, payload={}),
        agent_config=config,
        command_runner=runner,
    )
    assert other.error != "slot_not_allowlisted"
    assert other.http_status != 403
    assert runner.calls
    assert {serial for serial, _ in runner.calls} == {SLOT2_SERIAL}


def test_legacy_poc_slot_gate_still_slot1_only():
    from application.remote_access_farm_task import _poc_slot_gate

    config = _agent_config(remote_access_poc_enabled=True, remote_access_poc_slot_ids=(1,))
    blocked = _poc_slot_gate(config, 2)
    assert blocked is not None
    assert blocked.http_status == 403 and blocked.error == "slot_not_allowlisted"
    assert _poc_slot_gate(config, 1) is None


SLOT7_SERIAL = "SERIAL-SLOT7-TEST"
SLOT8_SERIAL = "SERIAL-SLOT8-TEST"
SLOT9_SERIAL = "SERIAL-SLOT9-TEST"
_GADS_WS = {1: "ws-1", 7: "ws-7", 8: "ws-8", 9: "ws-9"}
_GADS_MAP = {1: SLOT1_SERIAL, 7: SLOT7_SERIAL, 8: SLOT8_SERIAL, 9: SLOT9_SERIAL}


def _gads_multi(tmp_path: Path, tenant: MemoryTenant, **kwargs):
    platform = kwargs.pop("platform", None) or FakePlatform(
        registered={SLOT1_SERIAL, SLOT7_SERIAL, SLOT8_SERIAL, SLOT9_SERIAL}
    )
    platform.online.update({SLOT7_SERIAL: True, SLOT8_SERIAL: True, SLOT9_SERIAL: True})
    if not platform.device_workspace:
        platform.device_workspace = {
            SLOT1_SERIAL: "ws-1",
            SLOT7_SERIAL: "ws-7",
            SLOT8_SERIAL: "ws-8",
            SLOT9_SERIAL: "ws-9",
        }
    kwargs.setdefault("prepare_slot_ids", None)
    kwargs.setdefault("observe_slot_ids", None)
    return _service(
        tmp_path,
        tenant=tenant,
        platform=platform,
        allowed=tuple(range(1, 21)),
        gads_slot_ids=tuple(range(1, 21)),
        slot_map=_GADS_MAP,
        workspace_map=_GADS_WS,
        **kwargs,
    )


def test_phone8_owner_gets_own_gads_workspace(tmp_path: Path):
    tenant = MemoryTenant()
    service, _, _ = _gads_multi(tmp_path, tenant)
    rental = _rental(tenant, bay=8, user_id=CUSTOMER_A)
    result = service.create_remote_access(CUSTOMER_A, None, rental)
    assert result.http_status == 201, result.body
    assert result.body["slot_id"] == 8
    blob = json.dumps(result.body)
    assert SLOT8_SERIAL not in blob
    grants = [c for c in service._platform.calls if c[0] == "grant"]  # type: ignore[union-attr]
    assert grants[-1][1]["device_id"] == SLOT8_SERIAL
    assert grants[-1][1]["workspace_id"] == "ws-8"


def test_phone8_cannot_access_phone7_or_phone9(tmp_path: Path):
    tenant = MemoryTenant()
    service, _, _ = _gads_multi(tmp_path, tenant)
    rental8 = _rental(tenant, bay=8, user_id=CUSTOMER_A)
    rental7 = _rental(tenant, bay=7, user_id=CUSTOMER_B)
    rental9 = _rental(tenant, bay=9, user_id=CUSTOMER_B)
    assert service.create_remote_access(CUSTOMER_A, None, rental7).http_status == 403
    assert service.create_remote_access(CUSTOMER_A, None, rental9).http_status == 403
    granted = service.create_remote_access(CUSTOMER_A, None, rental8)
    assert granted.http_status == 201
    assert service.get_remote_access(CUSTOMER_A, None, rental7).http_status == 403
    assert service.get_remote_access(CUSTOMER_A, None, rental9).http_status == 403
    grants = [c for c in service._platform.calls if c[0] == "grant"]  # type: ignore[union-attr]
    assert all(c[1]["device_id"] != SLOT7_SERIAL for c in grants)
    assert all(c[1]["device_id"] != SLOT9_SERIAL for c in grants)


def test_two_rentals_cannot_share_phone8(tmp_path: Path):
    tenant = MemoryTenant()
    service, _, _ = _gads_multi(tmp_path, tenant)
    first = _rental(tenant, bay=8, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, first).http_status == 201
    second = str(uuid.uuid4())
    tenant.slots[8]["id"] = second
    tenant.slots[8]["rental_id"] = second
    tenant.slots[8]["user_id"] = CUSTOMER_B
    busy = service.create_remote_access(CUSTOMER_B, None, second)
    assert busy.http_status == 409
    assert busy.body["error"] == "remote_access_busy"


def test_phone8_and_phone9_concurrent_gads(tmp_path: Path):
    tenant = MemoryTenant()
    service, _, _ = _gads_multi(tmp_path, tenant)
    r8 = _rental(tenant, bay=8, user_id=CUSTOMER_A)
    r9 = _rental(tenant, bay=9, user_id=CUSTOMER_B)
    a = service.create_remote_access(CUSTOMER_A, None, r8)
    b = service.create_remote_access(CUSTOMER_B, None, r9)
    assert a.http_status == 201 and b.http_status == 201
    grants = [c[1] for c in service._platform.calls if c[0] == "grant"]  # type: ignore[union-attr]
    assert {g["workspace_id"] for g in grants} == {"ws-8", "ws-9"}
    assert {g["device_id"] for g in grants} == {SLOT8_SERIAL, SLOT9_SERIAL}


def test_revoked_phone8_session_is_inactive(tmp_path: Path):
    tenant = MemoryTenant()
    service, store, clock = _gads_multi(tmp_path, tenant)
    rental = _rental(tenant, bay=8, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    assert service.revoke_remote_access(CUSTOMER_A, None, rental).http_status == 200
    session = store.get(rental)
    assert session is None or not session.is_active(clock.now)
    listed = service.get_remote_access(CUSTOMER_A, None, rental)
    assert listed.http_status == 200
    assert listed.body.get("active") is False or listed.body.get("status") in {"none", "revoked", "expired"}


def test_expired_phone8_rental_cannot_open_gads(tmp_path: Path):
    tenant = MemoryTenant()
    clock = FakeClock()
    service, _, _ = _gads_multi(tmp_path, tenant, clock=clock)
    rental = _rental(tenant, bay=8, user_id=CUSTOMER_A, ends_at=clock.now - 60)
    result = service.create_remote_access(CUSTOMER_A, None, rental)
    assert result.http_status == 403
    assert [c for c in service._platform.calls if c[0] == "grant"] == []  # type: ignore[union-attr]


def test_unregistered_phone8_gads_device_fails_closed(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform(registered={SLOT1_SERIAL})
    service, _, _ = _gads_multi(tmp_path, tenant, platform=platform)
    rental = _rental(tenant, bay=8, user_id=CUSTOMER_A)
    result = service.create_remote_access(CUSTOMER_A, None, rental)
    assert result.http_status == 503
    assert result.body["error"] == "phone_unavailable"
    assert SLOT1_SERIAL not in json.dumps(result.body)


def test_phone8_wrong_gads_workspace_fails_closed(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform(registered={SLOT8_SERIAL})
    platform.device_workspace = {SLOT8_SERIAL: "ws-1"}
    service, _, _ = _gads_multi(tmp_path, tenant, platform=platform)
    rental = _rental(tenant, bay=8, user_id=CUSTOMER_A)
    result = service.create_remote_access(CUSTOMER_A, None, rental)
    assert result.http_status == 502


def test_shared_gads_workspace_is_rejected(tmp_path: Path):
    tenant = MemoryTenant()
    platform = FakePlatform(registered={SLOT7_SERIAL, SLOT8_SERIAL})
    service, _, _ = _service(
        tmp_path,
        tenant=tenant,
        platform=platform,
        allowed=tuple(range(1, 21)),
        gads_slot_ids=tuple(range(1, 21)),
        slot_map={7: SLOT7_SERIAL, 8: SLOT8_SERIAL},
        workspace_map={7: "shared-ws", 8: "shared-ws"},
    )
    r8 = _rental(tenant, bay=8, user_id=CUSTOMER_A)
    r7 = _rental(tenant, bay=7, user_id=CUSTOMER_B)
    assert service.create_remote_access(CUSTOMER_A, None, r8).http_status == 503
    assert service.create_remote_access(CUSTOMER_B, None, r7).http_status == 503
    assert platform.calls == []


def test_phone8_gads_enables_prepare_and_observe(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    farm.activation_details = {
        "verdict": "ACTIVATION_PARTIAL",
        "esim_profile_present": True,
        "esim_enabled": False,
        "network_registered": False,
        "cellular": False,
        "observation_complete": True,
    }
    service, _, _ = _gads_multi(tmp_path, tenant, farm=farm)
    rental = _rental(tenant, bay=8, user_id=CUSTOMER_A)
    created = service.create_remote_access(CUSTOMER_A, None, rental)
    assert created.http_status == 201, created.body
    prepared = service.prepare_esim(CUSTOMER_A, rental)
    assert prepared.http_status == 202, prepared.body
    observed = service.activation_status_for_customer(CUSTOMER_A, rental)
    assert observed.http_status == 200, observed.body
    assert any(t["type"] == "remote_access_activation_status" and t["slot"] == 8 for t in farm.tasks)
    blob = json.dumps(observed.body)
    assert SLOT8_SERIAL not in blob
    assert "ws-8" not in blob


def test_qr_upload_independent_of_gads_workspace_map(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    service, _, _ = _service(
        tmp_path,
        tenant=tenant,
        platform=FakePlatform(),
        farm=farm,
        allowed=(1,),
        gads_slot_ids=(1,),
        slot_map=_GADS_MAP,
        workspace_map={1: "ws-1"},
        farm_status=lambda: {
            "ok": True,
            "offline_slots": [],
            "mapped_slots": [1, 8],
            "slot_count": 2,
            "adb_online": 2,
        },
    )
    rental = _rental(tenant, bay=8, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 503
    uploaded = service.upload_esim_qr(CUSTOMER_A, rental, PNG_BYTES)
    assert uploaded.http_status == 200, uploaded.body
    assert farm.tasks[0]["slot"] == 8
    assert "serial" not in farm.tasks[0]["payload"]
