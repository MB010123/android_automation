"""Focused Farm Agent QR placement: download → slot_map serial → ADB Camera.

No GADS, no silent eSIM, no EuiccManager. Bytes only.
"""
from __future__ import annotations

import json
import os
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import requests
import base64

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.farm_task_types import FarmTaskRequest
from application.remote_access_farm_task import (
    _parse_remote_byte_size,
    run_remote_access_place_qr,
)
from application.remote_access_service import _qr_placement_succeeded
from infrastructure.adb_companion import AdbCommandError, AdbCommandResult, AdbCommandRunner
from tests.test_remote_access_poc import PNG_BYTES, _QrRunner, _agent_config

BAY1_SERIAL = "18171FDF6005WG"
PLACE_QR = "remote_access_place_qr"
QR_URL = "https://example.test/private/qr.png"
UNSAFE_MARKERS = (
    "Bearer ",
    "FARM_AGENT",
    "token=",
    "?token",
    "Authorization",
    QR_URL,
)


def _gif_bytes() -> bytes:
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (2, 2), color=(0, 0, 0)).save(buffer, format="GIF")
    return buffer.getvalue()


def _req(**payload: Any) -> FarmTaskRequest:
    body = {"esim_qr_url": QR_URL, "rental_id": "1dd479bd-a3a3-5220-87c9-bae7da81578a"}
    body.update(payload)
    return FarmTaskRequest(
        job_id="6d4acb6e-1111-2222-3333-444444444444",
        task_type=PLACE_QR,
        farm_slot_id=1,
        payload=body,
    )


def _place(runner: AdbCommandRunner, **kwargs: Any):
    config = kwargs.pop("agent_config", _agent_config(remote_access_poc_enabled=True))
    slot_map = kwargs.pop("slot_map", {1: BAY1_SERIAL})
    request = kwargs.pop("request", _req())
    downloader = kwargs.pop("downloader", lambda url, timeout: PNG_BYTES)
    return run_remote_access_place_qr(
        adb_path="adb",
        slot_map=slot_map,
        request=request,
        agent_config=config,
        command_runner=runner,
        downloader=downloader,
    )


def _assert_safe(result) -> None:
    blob = json.dumps({"error": result.error, "message": result.message, "details": result.details})
    for marker in UNSAFE_MARKERS:
        assert marker not in blob, marker


def test_parse_wc_c_and_stat_size():
    assert _parse_remote_byte_size("27333 /sdcard/DCIM/Camera/file.jpg") == 27333
    assert _parse_remote_byte_size("  8") == 8
    assert _parse_remote_byte_size("0 /sdcard/DCIM/Camera/empty.png") == 0
    assert _parse_remote_byte_size("") is None
    assert _parse_remote_byte_size("not-a-number") is None


def test_valid_qr_download_push_and_size():
    runner = _QrRunner(remote_size=len(PNG_BYTES))
    result = _place(runner)
    assert result.ok is True
    details = result.details or {}
    assert details["ok"] is True
    assert details["job_id"] == "6d4acb6e-1111-2222-3333-444444444444"
    assert details["serial"] == BAY1_SERIAL
    assert details["destination"] == (
        "/sdcard/DCIM/Camera/mobirent_esim_qr_1dd479bda3a3_6d4acb6e.png"
    )
    assert details["downloaded_size"] == len(PNG_BYTES)
    assert details["remote_size"] == len(PNG_BYTES)
    assert details["placed"] is True
    assert details["error_code"] is None
    assert result.message and result.message.startswith("qr_placed:")
    serials = {serial for serial, _ in runner.calls}
    assert serials == {BAY1_SERIAL}
    assert any(args[:3] == ["shell", "wc", "-c"] for _, args in runner.calls)
    assert any(args[:3] == ["shell", "mkdir", "-p"] and args[-1] == "/sdcard/DCIM/Camera" for _, args in runner.calls)
    _assert_safe(result)


def test_invalid_unfetchable_qr_url():
    runner = _QrRunner()

    def boom(url: str, timeout: float) -> bytes:
        raise requests.ConnectionError("refused")

    result = _place(runner, downloader=boom)
    assert result.ok is False
    assert result.error == "qr_download_failed"
    assert result.details["error_code"] == "qr_download_failed"
    assert result.details["placed"] is False
    assert runner.calls == []
    _assert_safe(result)

    empty = _place(_QrRunner(), downloader=lambda url, timeout: b"")
    assert empty.error == "qr_download_failed"
    assert empty.details["downloaded_size"] == 0
    assert empty.details["placed"] is False


def test_adb_serial_resolution_from_farm_slot_map():
    runner = _QrRunner(remote_size=len(PNG_BYTES))
    result = _place(runner, slot_map={1: BAY1_SERIAL, 2: "OTHER-SERIAL"})
    assert result.ok is True
    assert {serial for serial, _ in runner.calls} == {BAY1_SERIAL}
    assert result.details["serial"] == BAY1_SERIAL

    missing = _place(_QrRunner(), slot_map={2: BAY1_SERIAL})
    assert missing.ok is False
    assert missing.error == "slot_not_found"
    assert missing.details["placed"] is False


def test_successful_push_uses_camera_destination():
    runner = _QrRunner(remote_size=len(PNG_BYTES))
    result = _place(runner)
    push = [args for _, args in runner.calls if args and args[0] == "push"][0]
    assert push[2].startswith("/sdcard/DCIM/Camera/mobirent_esim_qr_")
    assert push[2].endswith(".png")
    assert result.details["destination"] == push[2]


def test_tempfile_is_written_to_disk_before_adb_push():
    class Inspect(_QrRunner):
        def __init__(self) -> None:
            super().__init__(remote_size=len(PNG_BYTES))
            self.local_size = 0

        def run(self, serial: str, arguments: list[str]):
            if arguments and arguments[0] == "push":
                self.local_size = os.path.getsize(arguments[1])
            return super().run(serial, arguments)

    runner = Inspect()
    result = _place(runner)
    assert result.ok is True
    assert runner.local_size == len(PNG_BYTES)
    assert runner.local_size > 0


def test_jpeg_extension_maps_to_jpg_destination():
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (2, 2), color=(255, 0, 0)).save(buffer, format="JPEG")
    jpeg = buffer.getvalue()
    runner = _QrRunner(remote_size=len(jpeg))
    result = _place(runner, downloader=lambda url, timeout: jpeg)
    assert result.ok is True
    assert str(result.details["destination"]).endswith(".jpg")


def test_zero_byte_push_is_failure():
    runner = _QrRunner(remote_size=0)
    result = _place(runner)
    assert result.ok is False
    assert result.error == "qr_zero_byte"
    assert result.details["placed"] is False
    assert result.details["remote_size"] == 0
    assert result.details["error_code"] == "qr_zero_byte"
    _assert_safe(result)


def test_missing_remote_file_after_push():
    runner = _QrRunner(ls_stdout="", remote_size=len(PNG_BYTES))
    result = _place(runner)
    assert result.ok is False
    assert result.error == "qr_not_on_device"
    assert result.details["placed"] is False
    _assert_safe(result)


class _LsRaisesRunner(_QrRunner):
    def run(self, serial: str, arguments: list[str]) -> AdbCommandResult:
        if arguments[:2] == ["shell", "ls"]:
            self.calls.append((serial, list(arguments)))
            raise AdbCommandError("qr_not_on_device")
        return super().run(serial, arguments)


def test_missing_remote_file_when_ls_fails():
    result = _place(_LsRaisesRunner())
    assert result.ok is False
    assert result.error == "qr_not_on_device"
    assert result.details["placed"] is False


def test_unsupported_gif_extension_rejected():
    runner = _QrRunner()
    result = _place(runner, downloader=lambda url, timeout: _gif_bytes())
    assert result.ok is False
    assert result.error == "qr_not_an_image"
    assert runner.calls == []
    _assert_safe(result)


def test_non_https_url_rejected_without_download():
    runner = _QrRunner()
    result = _place(
        runner,
        request=_req(esim_qr_url="http://example.test/qr.png"),
        downloader=lambda url, timeout: PNG_BYTES,
    )
    assert result.error == "invalid_assignment"
    assert runner.calls == []
    _assert_safe(result)


def test_safe_error_reporting_omits_url_and_tokens():
    runner = _QrRunner()
    result = _place(
        runner,
        request=_req(esim_qr_url="https://example.test/private/qr.png?token=super-secret"),
        downloader=lambda url, timeout: b"not-an-image",
    )
    assert result.error == "qr_not_an_image"
    blob = json.dumps({"error": result.error, "message": result.message, "details": result.details})
    assert "super-secret" not in blob
    assert "token=" not in blob
    _assert_safe(result)


def test_vps_success_gate_requires_placed_and_nonzero_remote_size():
    placed = SimpleNamespace(ok=True, error=None, body={"details": {"placed": True, "remote_size": 27333}})
    assert _qr_placement_succeeded(placed) is True
    zero = SimpleNamespace(ok=True, error=None, body={"details": {"placed": True, "remote_size": 0}})
    assert _qr_placement_succeeded(zero) is False
    missing = SimpleNamespace(ok=True, error=None, body={"details": {"placed": False, "remote_size": 8}})
    assert _qr_placement_succeeded(missing) is False
    legacy = SimpleNamespace(ok=True, error=None, body={"message": "qr_placed:/sdcard/DCIM/Camera/x.png"})
    assert _qr_placement_succeeded(legacy) is True


def test_inline_base64_image_skips_https_download():
    runner = _QrRunner(remote_size=len(PNG_BYTES))
    fetched: list[str] = []

    def boom(url: str, timeout: float) -> bytes:
        fetched.append(url)
        raise AssertionError("must not download when image_base64 is present")

    encoded = base64.b64encode(PNG_BYTES).decode("ascii")
    result = _place(
        runner,
        request=_req(image_base64=encoded),
        downloader=boom,
    )
    assert result.ok is True
    assert fetched == []
    assert result.details["serial"] == BAY1_SERIAL
    assert result.details["placed"] is True
    assert result.details["remote_size"] == len(PNG_BYTES)
    _assert_safe(result)


def test_invalid_inline_base64_is_rejected():
    runner = _QrRunner()
    result = _place(runner, request=_req(image_base64="%%%not-base64%%%"))
    assert result.ok is False
    assert result.error == "qr_not_an_image"
    assert runner.calls == []
