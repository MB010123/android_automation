"""Stream first-frame warmup, unbuffered MJPEG proxy, GADS 60/12/50 settings."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.device_stream_wake import DEVICE_STREAM_WAKE_TASK, run_device_stream_wake
from application.farm_agent_tasks import execute_farm_task, parse_farm_task_body
from application.farm_task_executor import FarmTaskExecutorDeps
from application.farm_task_types import FarmTaskRequest, SUPPORTED_TASK_TYPES
from application.gads_stream_settings import (
    JPEG_QUALITY,
    SCALING_FACTOR,
    TARGET_FPS,
    device_stream_settings_payload,
    global_stream_settings_payload,
    matches_live_info,
)
from application.stream_warmup import MjpegPrimeCache, PrimedMjpeg, start_stream_warmup
from infrastructure.adb_companion import AdbCommandResult, AdbCommandRunner
from infrastructure.gads_remote_access import GADS_HUB_ADMIN_USERNAME, GadsHubClient
from tests.test_remote_access_poc import (
    CUSTOMER_A,
    FakeFarm,
    FakeMjpeg,
    FakePlatform,
    SLOT1_SERIAL,
    _Resp,
    _rental,
    _service,
)
from tests.fakes_supabase import MemoryTenant
from tools.vps_backend_server import (
    MJPEG_CACHE_CONTROL,
    MJPEG_CHUNK_SIZE,
    mjpeg_proxy_headers,
)


class WakeRunner(AdbCommandRunner):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, list[str]]] = []

    def run(self, serial: str, arguments: list[str]) -> AdbCommandResult:
        self.calls.append((serial, arguments))
        return AdbCommandResult(stdout="", stderr="")


def test_gads_stream_settings_defaults_are_60_12_50():
    assert JPEG_QUALITY == 60
    assert TARGET_FPS == 12
    assert SCALING_FACTOR == 50
    assert device_stream_settings_payload() == {
        "jpeg_quality": 60,
        "target_fps": 12,
        "scaling_factor": 50,
    }
    assert global_stream_settings_payload()["scaling_factor_android"] == 50
    assert matches_live_info(
        {"stream_jpeg_quality": 60, "stream_target_fps": 12, "stream_scaling_factor": 50}
    )
    assert not matches_live_info(
        {"stream_jpeg_quality": 80, "stream_target_fps": 60, "stream_scaling_factor": 50}
    )


def test_gads_client_posts_global_and_device_stream_settings():
    seen: list[dict] = []

    class Session:
        def request(self, method, url, json=None, params=None, headers=None, timeout=None):
            seen.append(
                {"method": method, "url": url, "json": json, "timeout": timeout, "headers": headers}
            )
            if url.endswith("/authenticate"):
                return _Resp(200, {"success": True, "result": {"access_token": "jwt"}})
            return _Resp(200, {"success": True, "message": "ok"})

    client = GadsHubClient("http://hub.local:10000", admin_username="admin", admin_password="pw", session=Session())  # type: ignore[arg-type]
    client.update_global_stream_settings(global_stream_settings_payload())
    client.update_device_stream_settings(SLOT1_SERIAL, device_stream_settings_payload())
    paths = [row["url"] for row in seen]
    assert any(url.endswith("/admin/global-settings") for url in paths)
    assert any(url.endswith(f"/device/{SLOT1_SERIAL}/update-stream-settings") for url in paths)
    global_body = [row["json"] for row in seen if str(row["url"]).endswith("/admin/global-settings")][0]
    assert global_body["jpeg_quality"] == 60
    assert global_body["target_fps"] == 12
    device_body = [
        row["json"] for row in seen if str(row["url"]).endswith("/update-stream-settings")
    ][0]
    assert device_body == {"jpeg_quality": 60, "target_fps": 12, "scaling_factor": 50}


def test_stream_wake_task_sends_keyevent_224_only():
    assert DEVICE_STREAM_WAKE_TASK in SUPPORTED_TASK_TYPES
    request = parse_farm_task_body(
        {"job_id": "w1", "type": "device_stream_wake", "farm_slot_id": 21, "payload": {}}
    )
    runner = WakeRunner()
    result = run_device_stream_wake(
        adb_path="adb",
        slot_map={21: "RFCNB056WEV"},
        request=request,
        command_runner=runner,
    )
    assert result.ok is True
    assert runner.calls == [("RFCNB056WEV", ["shell", "input", "keyevent", "224"])]
    dispatched = execute_farm_task(
        adb_path="adb",
        slot_map={21: "RFCNB056WEV"},
        request=FarmTaskRequest(
            job_id="w2",
            task_type="device_stream_wake",
            farm_slot_id=21,
            payload={},
        ),
        deps=FarmTaskExecutorDeps(command_runner=runner),
    )
    assert dispatched.ok is True


def test_primer_handoff_returns_first_chunk_without_second_open():
    platform = FakePlatform()
    primer = MjpegPrimeCache()
    ran: list[str] = []

    def after() -> None:
        ran.append("after")

    start_stream_warmup(
        primer=primer,
        background=lambda fn: fn(),
        farm=FakeFarm(),
        platform=platform,
        slot_id=1,
        device_id=SLOT1_SERIAL,
        platform_username=GADS_HUB_ADMIN_USERNAME,
        platform_password="",
        after=after,
    )
    assert ran == ["after"]
    assert [c[0] for c in platform.calls] == ["stream"]
    primed = primer.take(SLOT1_SERIAL, wait_seconds=0)
    assert isinstance(primed, PrimedMjpeg)
    assert primed.status_code == 200
    chunks = list(primed.iter_content(4096))
    assert chunks and chunks[0].startswith(b"--frame")
    assert primer.take(SLOT1_SERIAL, wait_seconds=0) is None


def test_mjpeg_proxy_headers_disable_nginx_and_browser_cache():
    assert MJPEG_CHUNK_SIZE == 4096
    headers = dict(mjpeg_proxy_headers("multipart/x-mixed-replace; boundary=frame"))
    assert headers["X-Accel-Buffering"] == "no"
    assert headers["Cache-Control"] == MJPEG_CACHE_CONTROL
    assert "no-store" in headers["Cache-Control"]
    assert "must-revalidate" in headers["Cache-Control"]
    assert headers["Pragma"] == "no-cache"


def test_customer_create_wakes_and_primes_before_stream_get(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    platform = FakePlatform()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    created = service.create_remote_access(CUSTOMER_A, None, rental)
    assert created.http_status == 201
    assert any(t["type"] == "device_stream_wake" for t in farm.tasks)
    assert any(c[0] == "stream" for c in platform.calls)
    access_after_create = [t for t in farm.tasks if t["type"] == "device_access_mode"]
    stream = service.open_stream(CUSTOMER_A, rental)
    assert isinstance(stream, (FakeMjpeg, PrimedMjpeg)) or getattr(stream, "status_code", None) == 200
    assert [t for t in farm.tasks if t["type"] == "device_access_mode"] == access_after_create
