"""Customer POST /rentals/{rental_id}/esim/upload → Farm Camera placement.

No GADS session, no assign, no silent eSIM.
"""
from __future__ import annotations

import json
import sys
import uuid
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.esim_qr_upload import parse_qr_image_multipart, public_placement_success, qr_upload_error_body
from application.remote_access_farm_task import MAX_QR_IMAGE_BYTES
from application.remote_access_service import REMOTE_ACCESS_PLACE_QR_TASK
from infrastructure.farm_task_client import FarmTaskResponse
from tests.fakes_supabase import MemoryTenant
from tests.test_remote_access_poc import (
    CUSTOMER_A,
    CUSTOMER_B,
    FARM_TOKEN,
    PNG_BYTES,
    SLOT1_SERIAL,
    SLOT2_SERIAL,
    FakeFarm,
    FakePlatform,
    _rental,
    _service,
    _signup,
    _start_http,
)

BAY1_SERIAL = "18171FDF6005WG"


def _jpeg_bytes() -> bytes:
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (4, 4), color=(10, 20, 30)).save(buf, format="JPEG")
    return buf.getvalue()


def _webp_bytes() -> bytes:
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (4, 4), color=(40, 50, 60)).save(buf, format="WEBP")
    return buf.getvalue()


def _gif_bytes() -> bytes:
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (2, 2), color=(0, 0, 0)).save(buf, format="GIF")
    return buf.getvalue()


def _multipart(filename: str, content_type: str, data: bytes, *, field: str = "qr_image") -> tuple[bytes, str]:
    boundary = "----TestBoundaryQrUpload"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8") + data + f"\r\n--{boundary}--\r\n".encode("utf-8")
    return body, f"multipart/form-data; boundary={boundary}"


def _http_raw(method: str, url: str, *, token: str | None, data: bytes | None, content_type: str | None):
    headers: dict[str, str] = {}
    if content_type:
        headers["Content-Type"] = content_type
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status, json.loads(resp.read().decode()), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        return exc.code, (json.loads(raw) if raw else {}), dict(exc.headers)


def _assert_safe_body(body: dict) -> None:
    blob = json.dumps(body)
    for marker in ("Bearer ", "FARM_AGENT", "MUST-NOT-LEAK", "token=", BAY1_SERIAL, SLOT1_SERIAL):
        assert marker not in blob, marker


def test_parse_multipart_extracts_qr_image():
    body, ctype = _multipart("qr.png", "image/png", PNG_BYTES)
    parsed = parse_qr_image_multipart(ctype, body)
    assert parsed == PNG_BYTES
    assert parse_qr_image_multipart("application/json", b"{}") is None
    wrong, ctype2 = _multipart("qr.png", "image/png", PNG_BYTES, field="file")
    assert parse_qr_image_multipart(ctype2, wrong) is None


def test_parse_multipart_quoted_boundary_and_extra_fields():
    boundary = "----WebKitFormBoundaryQuoted"
    extra = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="note"\r\n\r\n'
        "not-the-image\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="qr_image"; filename="photo.jpg"\r\n'
        "Content-Type: image/jpeg\r\n\r\n"
    ).encode("utf-8") + PNG_BYTES + f"\r\n--{boundary}--\r\n".encode("utf-8")
    parsed = parse_qr_image_multipart(f'multipart/form-data; charset=utf-8; boundary="{boundary}"', extra)
    assert parsed == PNG_BYTES


def test_parse_multipart_rejects_json_and_path_only():
    body, ctype = _multipart("qr.png", "application/json", b'{"url":"https://example.supabase.co/qr.png"}')
    parsed = parse_qr_image_multipart(ctype, body)
    assert parsed == b'{"url":"https://example.supabase.co/qr.png"}'
    assert parse_qr_image_multipart("multipart/form-data", b"") is None
    json_only = parse_qr_image_multipart("application/json; charset=utf-8", b'{"qr_image":"x"}')
    assert json_only is None


def test_public_success_omits_serial():
    body = public_placement_success(
        {
            "placed": True,
            "serial": BAY1_SERIAL,
            "destination": "/sdcard/DCIM/Camera/x.png",
            "downloaded_size": 8,
            "remote_size": 8,
        },
        job_id="job-1",
    )
    assert body["ok"] is True
    assert body["placed"] is True
    assert body["job_id"] == "job-1"
    assert "serial" not in body
    assert body["destination"].startswith("/sdcard/DCIM/Camera/")
    err = qr_upload_error_body("qr_not_an_image")
    assert err["ok"] is False and err["step"] == "qr_upload" and err["error_code"] == "qr_not_an_image"


def test_upload_requires_ownership(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    stolen = service.upload_esim_qr(CUSTOMER_B, rental, PNG_BYTES)
    assert stolen.http_status == 403
    assert stolen.body["error_code"] == "rental_not_owned"
    assert farm.tasks == []
    ok = service.upload_esim_qr(CUSTOMER_A, rental, PNG_BYTES)
    assert ok.http_status == 200
    assert ok.body["placed"] is True
    assert "serial" not in ok.body
    assert farm.tasks[0]["type"] == "remote_access_place_qr"
    assert farm.tasks[0]["slot"] == 1
    assert "image_base64" in farm.tasks[0]["payload"]
    assert "esim_qr_url" not in farm.tasks[0]["payload"]
    _assert_safe_body(ok.body)


def test_upload_png_jpeg_webp(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    for blob in (PNG_BYTES, _jpeg_bytes(), _webp_bytes()):
        farm.tasks.clear()
        result = service.upload_esim_qr(CUSTOMER_A, rental, blob)
        assert result.http_status == 200, result.body
        assert result.body["downloaded_size"] == len(blob)
        assert result.body["remote_size"] == len(blob)


def test_upload_rejects_invalid_and_oversized(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    bad = service.upload_esim_qr(CUSTOMER_A, rental, _gif_bytes())
    assert bad.http_status == 400 and bad.body["error_code"] == "qr_not_an_image"
    huge = service.upload_esim_qr(CUSTOMER_A, rental, b"x" * (MAX_QR_IMAGE_BYTES + 1))
    assert huge.http_status == 413 and huge.body["error_code"] == "qr_image_too_large"
    empty = service.upload_esim_qr(CUSTOMER_A, rental, b"")
    assert empty.http_status == 400 and empty.body["error_code"] == "qr_upload_missing"
    assert farm.tasks == []


def test_upload_maps_zero_byte_and_missing_remote(tmp_path: Path):
    class SizedFarm(FakeFarm):
        def __init__(self, details: dict) -> None:
            super().__init__()
            self._details = details

        def run_task(self, *, task_type: str, farm_slot_id: int, payload: dict, job_id: str) -> FarmTaskResponse:
            self.tasks.append({"type": task_type, "slot": farm_slot_id, "payload": payload, "job_id": job_id})
            return FarmTaskResponse(
                ok=False,
                http_status=422,
                error=str(self._details.get("error_code")),
                body={"details": self._details},
            )

    tenant = MemoryTenant()
    zero_farm = SizedFarm({"placed": False, "remote_size": 0, "error_code": "qr_zero_byte"})
    service, _, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=zero_farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    zero = service.upload_esim_qr(CUSTOMER_A, rental, PNG_BYTES)
    assert zero.body["error_code"] == "qr_zero_byte"
    assert zero.body["ok"] is False and zero.body["step"] == "qr_upload"

    missing_farm = SizedFarm({"placed": False, "error_code": "qr_not_on_device"})
    service2, _, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=missing_farm)
    missing = service2.upload_esim_qr(CUSTOMER_A, rental, PNG_BYTES)
    assert missing.body["error_code"] == "qr_not_on_device"


def test_upload_does_not_need_gads_session(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    service, store, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm)
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    assert store.get(rental) is None
    result = service.upload_esim_qr(CUSTOMER_A, rental, PNG_BYTES)
    assert result.http_status == 200
    assert store.get(rental) is None
    assert farm.tasks[0]["type"] == REMOTE_ACCESS_PLACE_QR_TASK


def _mapped(*bays: int):
    listed = list(bays)
    return lambda: {
        "ok": True,
        "offline_slots": [],
        "mapped_slots": listed,
        "slot_count": len(listed),
        "adb_online": len(listed),
    }


def test_owned_slot1_qr_upload_still_succeeds(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm, farm_status=_mapped(1))
    rental = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    result = service.upload_esim_qr(CUSTOMER_A, rental, PNG_BYTES)
    assert result.http_status == 200
    assert farm.tasks[0]["slot"] == 1
    assert "serial" not in farm.tasks[0]["payload"]
    assert "image_base64" in farm.tasks[0]["payload"]
    assert farm.tasks[0]["payload"]["rental_id"] == rental
    _assert_safe_body(result.body)


def test_owned_slot2_qr_upload_succeeds(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    service, _, _ = _service(
        tmp_path,
        tenant=tenant,
        platform=FakePlatform(),
        farm=farm,
        allowed=(1,),
        farm_status=_mapped(1, 2),
    )
    rental = _rental(tenant, bay=2, user_id=CUSTOMER_A)
    gads = service.create_remote_access(CUSTOMER_A, None, rental)
    assert gads.http_status == 503
    assert gads.body["error"] == "phone_unavailable"
    result = service.upload_esim_qr(CUSTOMER_A, rental, PNG_BYTES)
    assert result.http_status == 200, result.body
    assert result.body["placed"] is True
    assert "serial" not in result.body
    assert SLOT2_SERIAL not in json.dumps(result.body)
    assert farm.tasks[-1]["type"] == REMOTE_ACCESS_PLACE_QR_TASK
    assert farm.tasks[-1]["slot"] == 2
    assert "serial" not in farm.tasks[-1]["payload"]
    assert "udid" not in farm.tasks[-1]["payload"]
    assert "image_base64" in farm.tasks[-1]["payload"]
    assert farm.tasks[-1]["payload"]["rental_id"] == rental
    _assert_safe_body(result.body)


def test_owned_slots_3_to_20_follow_mapped_bay_authorization(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    service, _, _ = _service(
        tmp_path,
        tenant=tenant,
        platform=FakePlatform(),
        farm=farm,
        allowed=(1,),
        farm_status=_mapped(*range(1, 21)),
    )
    for bay in range(3, 21):
        farm.tasks.clear()
        rental = _rental(tenant, bay=bay, user_id=CUSTOMER_A)
        assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 503
        result = service.upload_esim_qr(CUSTOMER_A, rental, PNG_BYTES)
        assert result.http_status == 200, (bay, result.body)
        assert farm.tasks[0]["slot"] == bay
        assert "serial" not in farm.tasks[0]["payload"]


def test_non_owner_slot2_qr_upload_is_forbidden(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm, farm_status=_mapped(1, 2))
    rental = _rental(tenant, bay=2, user_id=CUSTOMER_A)
    stolen = service.upload_esim_qr(CUSTOMER_B, rental, PNG_BYTES)
    assert stolen.http_status == 403
    assert stolen.body["error_code"] == "rental_not_owned"
    assert stolen.body["step"] == "qr_upload"
    assert farm.tasks == []


def test_unmapped_bay_cannot_upload_qr(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm, farm_status=_mapped(1))
    rental = _rental(tenant, bay=2, user_id=CUSTOMER_A)
    result = service.upload_esim_qr(CUSTOMER_A, rental, PNG_BYTES)
    assert result.http_status == 503
    assert result.body["error_code"] == "phone_unavailable"
    assert farm.tasks == []


def test_unknown_rental_qr_upload_is_rejected(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    service, _, _ = _service(tmp_path, tenant=tenant, platform=FakePlatform(), farm=farm, farm_status=_mapped(1, 2))
    missing = service.upload_esim_qr(CUSTOMER_A, str(uuid.uuid4()), PNG_BYTES)
    assert missing.http_status == 404
    assert missing.body["error_code"] == "rental_not_found"
    assert farm.tasks == []


def test_slot2_gads_stays_blocked_slot1_gads_unchanged(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    platform = FakePlatform(registered={SLOT1_SERIAL, SLOT2_SERIAL})
    service, _, _ = _service(tmp_path, tenant=tenant, platform=platform, farm=farm, allowed=(1,), farm_status=_mapped(1, 2))
    rental1 = _rental(tenant, bay=1, user_id=CUSTOMER_A)
    rental2 = _rental(tenant, bay=2, user_id=CUSTOMER_A)
    granted = service.create_remote_access(CUSTOMER_A, None, rental1)
    assert granted.http_status == 201
    blocked = service.create_remote_access(CUSTOMER_A, None, rental2)
    assert blocked.http_status == 503
    assert blocked.body.get("error") == "phone_unavailable"
    assert service.get_device_status(2).http_status == 503
    assert [c[0] for c in platform.calls if c[0] == "grant"]
    assert not any(c[0] == "grant" and c[1].get("device_id") == SLOT2_SERIAL for c in platform.calls)


def test_http_authenticated_upload_and_isolation(tmp_path: Path):
    server, port, tenant, _platform, handler = _start_http(tmp_path)
    base = f"http://127.0.0.1:{port}"
    try:
        token_a, user_a = _signup(base, "upload-a@example.com")
        token_b, _user_b = _signup(base, "upload-b@example.com")
        rental = _rental(tenant, bay=1, user_id=user_a)
        url = f"{base}/rentals/{rental}/esim/upload"
        body, ctype = _multipart("qr.png", "image/png", PNG_BYTES)

        status, payload, _ = _http_raw("POST", url, token=None, data=body, content_type=ctype)
        assert status == 401

        status, payload, _ = _http_raw("POST", url, token=FARM_TOKEN, data=body, content_type=ctype)
        assert status == 401

        status, payload, _ = _http_raw("POST", url, token=token_b, data=body, content_type=ctype)
        assert status == 403
        assert payload["step"] == "qr_upload"

        status, payload, _ = _http_raw("POST", url, token=token_a, data=body, content_type=ctype)
        assert status == 200, payload
        assert payload["ok"] is True and payload["placed"] is True
        assert payload["destination"].startswith("/sdcard/DCIM/Camera/")
        assert payload["downloaded_size"] == len(PNG_BYTES)
        assert payload["remote_size"] == len(PNG_BYTES)
        assert "serial" not in payload
        _assert_safe_body(payload)
        farm = handler.remote_access_service._farm
        assert farm.tasks[-1]["slot"] == 1
        assert "image_base64" in farm.tasks[-1]["payload"]

        bad_body, bad_ctype = _multipart("qr.gif", "image/gif", _gif_bytes())
        status, payload, _ = _http_raw("POST", url, token=token_a, data=bad_body, content_type=bad_ctype)
        assert status == 400 and payload["error_code"] == "qr_not_an_image"

        status, payload, _ = _http_raw("POST", url, token=token_a, data=b"{}", content_type="application/json")
        assert status == 400 and payload["error_code"] == "qr_upload_missing"
    finally:
        server.shutdown()


def test_http_upload_without_poc_is_forbidden(tmp_path: Path):
    server, port, tenant, _platform, _ = _start_http(tmp_path, with_service=False)
    base = f"http://127.0.0.1:{port}"
    try:
        token_a, user_a = _signup(base, "upload-off@example.com")
        rental = _rental(tenant, bay=1, user_id=user_a)
        body, ctype = _multipart("qr.png", "image/png", PNG_BYTES)
        status, payload, _ = _http_raw(
            "POST",
            f"{base}/rentals/{rental}/esim/upload",
            token=token_a,
            data=body,
            content_type=ctype,
        )
        assert status == 403
        assert payload.get("error_code") == "forbidden" or payload.get("error") == "forbidden"
    finally:
        server.shutdown()
