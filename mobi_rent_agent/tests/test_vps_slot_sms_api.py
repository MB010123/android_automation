"""VPS Lovable slot SMS API (mocked Farm; no live SMS)."""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.vps_slot_sms_service import VpsSlotSmsService
from infrastructure.farm_sms_client import FarmDispatchResponse
from infrastructure.outbound_message_store import OutboundMessageStore
from infrastructure.slot_public_id import public_id_for_farm_slot
from infrastructure.vps_rate_limiter import VpsRateLimiter

SLOT1 = public_id_for_farm_slot(1)
SLOT2 = public_id_for_farm_slot(2)
TO = "+14695550182"


class MockFarmClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail = False
        self.lock = threading.Lock()

    def send_sms(self, **kwargs: object) -> FarmDispatchResponse:
        with self.lock:
            self.calls.append(dict(kwargs))
        if self.fail:
            return FarmDispatchResponse(ok=False, http_status=422, body={}, error="blocked")
        return FarmDispatchResponse(
            ok=True,
            http_status=200,
            body={"ok": True, "status": "sent", "provider_message_id": "mock-provider-1"},
        )


def _service(
    tmp_path: Path,
    *,
    farm: MockFarmClient | None = None,
    offline: list[int] | None = None,
    farm_unreachable: bool = False,
    known_slots: set[int] | None = None,
    rate: VpsRateLimiter | None = None,
) -> VpsSlotSmsService:
    store = OutboundMessageStore(tmp_path / "api.sqlite")

    def status() -> dict:
        if farm_unreachable:
            raise ConnectionError("farm down")
        return {
            "ok": True,
            "role": "farm",
            "slot_count": 20,
            "adb_online": 20,
            "offline_slots": offline or [],
        }

    return VpsSlotSmsService(
        message_store=store,
        farm_client=farm,
        farm_status_fetcher=status,
        known_farm_slots=known_slots or {1, 2},
        rate_limiter=rate,
        max_dispatch_attempts=1,
    )


def _payload(**overrides: object) -> dict:
    base = {"to": TO, "body": "Hello from Mobi-Rent", "idempotency_key": "abc-123"}
    base.update(overrides)
    return base


def test_auth_missing_token_on_handler(tmp_path: Path):
    path = ROOT / "tools" / "vps_backend_server.py"
    spec = importlib.util.spec_from_file_location("vps_backend_server", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    Handler = mod.Handler
    Handler.farm_service_token = "service-secret"
    Handler.slot_sms_service = _service(tmp_path, farm=MockFarmClient())
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/slots/{SLOT1}/sms/send",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=3)
            pytest.fail("expected 401")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
    finally:
        server.shutdown()


def test_validation_errors(tmp_path: Path):
    svc = _service(tmp_path, farm=MockFarmClient())
    assert svc.enqueue_send(SLOT1, _payload(to="bad")).http_status == 400
    assert svc.enqueue_send(SLOT1, _payload(body="")).http_status == 400
    assert svc.enqueue_send(SLOT1, {"to": TO, "body": "x"}).http_status == 400
    assert svc.enqueue_send("00000000-0000-0000-0000-000000000099", _payload()).http_status == 404


def test_offline_and_farm_unreachable(tmp_path: Path):
    svc = _service(tmp_path, farm=MockFarmClient(), offline=[1])
    assert svc.enqueue_send(SLOT1, _payload()).http_status == 409
    svc2 = _service(tmp_path / "b", farm=MockFarmClient(), farm_unreachable=True)
    assert svc2.enqueue_send(SLOT1, _payload()).http_status == 503


def test_idempotency_same_message_id(tmp_path: Path):
    farm = MockFarmClient()
    svc = _service(tmp_path, farm=farm)
    first = svc.enqueue_send(SLOT1, _payload())
    second = svc.enqueue_send(SLOT1, _payload())
    assert first.http_status == 202
    assert second.http_status == 202
    assert first.body["message_id"] == second.body["message_id"]
    time.sleep(0.3)
    assert len(farm.calls) == 1


def test_idempotency_conflict(tmp_path: Path):
    svc = _service(tmp_path, farm=MockFarmClient())
    svc.enqueue_send(SLOT1, _payload(body="one"))
    conflict = svc.enqueue_send(SLOT1, _payload(body="two"))
    assert conflict.http_status == 409
    assert conflict.body["error"] == "idempotency_conflict"


def test_concurrent_duplicate_single_job(tmp_path: Path):
    farm = MockFarmClient()
    svc = _service(tmp_path, farm=farm)
    barrier = threading.Barrier(2)
    results: list = []

    def worker() -> None:
        barrier.wait()
        results.append(svc.enqueue_send(SLOT1, _payload(idempotency_key="race-key")))

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    ids = {r.body["message_id"] for r in results if r.http_status == 202}
    assert len(ids) == 1
    deadline = time.time() + 2.0
    while time.time() < deadline and len(farm.calls) < 1:
        time.sleep(0.05)
    assert len(farm.calls) == 1


def test_async_dispatch_and_get_message(tmp_path: Path):
    farm = MockFarmClient()
    svc = _service(tmp_path, farm=farm)
    enq = svc.enqueue_send(SLOT1, _payload(idempotency_key="e2e-key"))
    assert enq.http_status == 202
    assert enq.body["status"] == "queued"
    mid = enq.body["message_id"]
    deadline = time.time() + 2.0
    while time.time() < deadline:
        got = svc.get_message(mid)
        if got.body.get("status") == "sent":
            break
        time.sleep(0.05)
    assert got.http_status == 200
    assert got.body["status"] == "sent"
    assert len(farm.calls) == 1
    assert farm.calls[0]["sender_slot_id"] == 1
    assert farm.calls[0]["to_number"] == TO


def test_farm_failure_marks_failed(tmp_path: Path):
    farm = MockFarmClient()
    farm.fail = True
    svc = _service(tmp_path, farm=farm)
    enq = svc.enqueue_send(SLOT1, _payload(idempotency_key="fail-key"))
    assert enq.http_status == 202
    mid = enq.body["message_id"]
    deadline = time.time() + 2.0
    while time.time() < deadline:
        got = svc.get_message(mid)
        if got.body.get("status") == "failed":
            break
        time.sleep(0.05)
    assert got.body["status"] == "failed"


def test_rate_limited(tmp_path: Path):
    rate = VpsRateLimiter(per_slot_limit=1, per_slot_window_seconds=60.0, global_limit=1000)
    svc = _service(tmp_path, farm=MockFarmClient(), rate=rate)
    assert svc.enqueue_send(SLOT1, _payload(idempotency_key="r1")).http_status == 202
    limited = svc.enqueue_send(SLOT1, _payload(idempotency_key="r2"))
    assert limited.http_status == 429
    assert limited.body["error"] == "rate_limited"
    assert limited.body["retry_after"] >= 1


def test_list_messages_and_unknown_message(tmp_path: Path):
    farm = MockFarmClient()
    svc = _service(tmp_path, farm=farm)
    enq = svc.enqueue_send(SLOT1, _payload(idempotency_key="list-key"))
    mid = enq.body["message_id"]
    time.sleep(0.3)
    listing = svc.list_messages(SLOT1, limit=10)
    assert listing.http_status == 200
    assert any(m["message_id"] == mid for m in listing.body["messages"])
    missing = svc.get_message("00000000-0000-0000-0000-000000000099")
    assert missing.http_status == 404


def test_mocked_http_e2e(tmp_path: Path):
    path = ROOT / "tools" / "vps_backend_server.py"
    spec = importlib.util.spec_from_file_location("vps_backend_server", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    Handler = mod.Handler
    farm = MockFarmClient()
    svc = _service(tmp_path, farm=farm)
    Handler.farm_service_token = "service-secret"
    Handler.slot_sms_service = svc
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body = json.dumps(_payload(idempotency_key="http-e2e")).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/slots/{SLOT1}/sms/send",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer service-secret",
            },
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            assert resp.status == 202
            created = json.loads(resp.read().decode())
        mid = created["message_id"]
        time.sleep(0.4)
        greq = urllib.request.Request(
            f"http://127.0.0.1:{port}/messages/{mid}",
            headers={"Authorization": "Bearer service-secret"},
        )
        with urllib.request.urlopen(greq, timeout=3) as resp:
            detail = json.loads(resp.read().decode())
        assert detail["status"] == "sent"
        assert len(farm.calls) == 1
    finally:
        server.shutdown()
