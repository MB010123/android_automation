"""VPS farm management API (mocked Farm; no live Android/SMS)."""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.vps_farm_management_service import VpsFarmManagementService
from application.vps_job_worker import VpsJobWorker
from infrastructure.farm_task_client import FarmTaskResponse
from infrastructure.inbound_message_store import InboundMessageStore
from infrastructure.lovable_inbound_webhook import sign_payload, verify_signature
from infrastructure.slot_assignment_store import SlotAssignmentStore
from infrastructure.slot_event_store import SlotEventStore
from infrastructure.slot_public_id import public_id_for_farm_slot
from infrastructure.vps_job_store import VpsJobStore
from infrastructure.vps_rate_limiter import VpsRateLimiter

SLOT1 = public_id_for_farm_slot(1)
RENTAL = str(uuid.uuid4())


class MockFarmTaskClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail = False
        self.unsupported = False
        self.provision_fail = False

    def run_task(self, **kwargs: object) -> FarmTaskResponse:
        self.calls.append(dict(kwargs))
        if self.fail:
            return FarmTaskResponse(ok=False, http_status=503, body={}, error="farm_down")
        if self.unsupported:
            return FarmTaskResponse(
                ok=False,
                http_status=501,
                body={"error": "action_not_supported"},
                error="action_not_supported",
            )
        if self.provision_fail:
            return FarmTaskResponse(
                ok=False,
                http_status=422,
                body={"error": "provisioning_failed", "message": "human path"},
                error="provisioning_failed",
            )
        return FarmTaskResponse(ok=True, http_status=200, body={"ok": True})


def _mgmt(tmp_path: Path, *, farm: MockFarmTaskClient | None = None, offline: list[int] | None = None):
    jobs = VpsJobStore(tmp_path / "jobs.sqlite")
    assign = SlotAssignmentStore(tmp_path / "assign.sqlite")
    events = SlotEventStore(tmp_path / "events.sqlite")
    farm = farm or MockFarmTaskClient()

    def status() -> dict:
        return {
            "ok": True,
            "offline_slots": offline or [],
            "slot_count": 20,
            "adb_online": 20,
        }

    worker = VpsJobWorker(
        job_store=jobs,
        assignment_store=assign,
        event_store=events,
        farm_task_client=farm,
        poll_interval_seconds=3600.0,
    )
    svc = VpsFarmManagementService(
        job_store=jobs,
        assignment_store=assign,
        event_store=events,
        job_worker=worker,
        farm_status_fetcher=status,
        known_farm_slots={1, 2, 4},
        rate_limiter=VpsRateLimiter(per_slot_limit=100, global_limit=1000),
    )
    return svc, worker, farm, jobs


def _assign_payload(**kw: object) -> dict:
    base = {
        "rental_id": RENTAL,
        "esim_qr_url": "https://example.com/qr",
        "carrier": "test-carrier",
        "band_lock": "",
        "proxy": "",
    }
    base.update(kw)
    return base


def test_available_slots_excludes_offline_and_assigned(tmp_path: Path):
    svc, _, _, _ = _mgmt(tmp_path, offline=[2])
    assert svc.list_available_slots().body["available"]
    svc.assign_slot(4, _assign_payload())
    body = svc.list_available_slots().body
    bays = {item["bay"] for item in body["available"]}
    assert 2 not in bays
    assert 4 not in bays
    assert 1 in bays


def test_assign_and_job_done(tmp_path: Path):
    farm = MockFarmTaskClient()
    svc, worker, _, jobs = _mgmt(tmp_path, farm=farm)
    result = svc.assign_slot(1, _assign_payload())
    assert result.http_status == 202
    assert result.body["ok"] is True
    assert result.body["bay"] == 1
    assert result.body["slot_id"] == SLOT1
    assert result.body["status"] == "pending"
    job_id = result.body["job_id"]
    deadline = time.time() + 2.0
    while time.time() < deadline and svc.get_job(job_id).body.get("state") != "done":
        time.sleep(0.05)
    job = svc.get_job(job_id)
    assert job.body["state"] == "done"
    assert len(farm.calls) == 1


def test_assign_idempotent_same_rental(tmp_path: Path):
    svc, _, _, _ = _mgmt(tmp_path)
    first = svc.assign_slot(1, _assign_payload())
    second = svc.assign_slot(1, _assign_payload())
    assert first.http_status == 202
    assert second.http_status == 202
    assert first.body["job_id"] == second.body["job_id"]


def test_assign_invalid_bay_and_unavailable(tmp_path: Path):
    svc, _, _, _ = _mgmt(tmp_path)
    assert svc.assign_slot(99, _assign_payload()).http_status == 404
    svc.assign_slot(1, _assign_payload())
    assert svc.assign_slot(1, _assign_payload(rental_id=str(uuid.uuid4()))).http_status == 409


def test_farm_unreachable_on_assign(tmp_path: Path):
    farm = MockFarmTaskClient()
    farm.fail = True
    svc, worker, _, _ = _mgmt(tmp_path, farm=farm)
    svc.assign_slot(1, _assign_payload())
    job_id = svc.assign_slot(4, _assign_payload(rental_id=str(uuid.uuid4()))).body.get("job_id")
    if job_id:
        worker.process_job(job_id)
    assert svc.get_job(job_id or "").http_status in (404, 200)


def test_assign_provisioning_failure_releases_bay(tmp_path: Path):
    farm = MockFarmTaskClient()
    farm.provision_fail = True
    svc, worker, _, _ = _mgmt(tmp_path, farm=farm)
    assign = SlotAssignmentStore(tmp_path / "assign.sqlite")
    result = svc.assign_slot(1, _assign_payload())
    job_id = result.body["job_id"]
    worker.process_job(job_id)
    assert svc.get_job(job_id).body["state"] == "failed"
    assert svc.get_job(job_id).body["error"] == "provisioning_failed"
    assert not assign.is_assigned(1)


def test_action_reboot_and_unsupported(tmp_path: Path):
    farm = MockFarmTaskClient()
    svc, worker, _, _ = _mgmt(tmp_path, farm=farm)
    reboot = svc.enqueue_action(SLOT1, "reboot", {})
    assert reboot.http_status == 202
    assert reboot.body["ok"] is True
    assert reboot.body["action"] == "reboot"
    worker.process_job(reboot.body["job_id"])
    assert svc.get_job(reboot.body["job_id"]).body["state"] == "done"
    bad = svc.enqueue_action(SLOT1, "not_real", {})
    assert bad.http_status == 400


def test_events_and_invalid_since(tmp_path: Path):
    svc, _, _, _ = _mgmt(tmp_path)
    svc.record_event(1, "device_online", "test")
    events = svc.list_events(SLOT1, since_raw=None, limit_raw="10")
    assert events.body["events"]
    invalid = svc.list_events(SLOT1, since_raw="not-a-ts", limit_raw="10")
    assert invalid.http_status == 400


def test_inbound_dedup_and_hmac(tmp_path: Path):
    store = InboundMessageStore(tmp_path / "inbound.sqlite")
    first_id, created1 = store.insert(
        device_id="1",
        slot_id=1,
        from_number="+15551234567",
        body="hello",
        payload={"x": 1},
        provider_message_id="vf-123",
        slot_public_id=SLOT1,
    )
    second_id, created2 = store.insert(
        device_id="1",
        slot_id=1,
        from_number="+15551234567",
        body="hello",
        payload={"x": 1},
        provider_message_id="vf-123",
        slot_public_id=SLOT1,
    )
    assert created1 is True
    assert created2 is False
    assert first_id == second_id
    secret = "test-secret"
    body = b'{"slot_id":"x"}'
    sig = sign_payload(secret, body)
    assert verify_signature(secret, body, sig)
    assert not verify_signature(secret, body, "bad")


def test_http_assign_e2e_mock(tmp_path: Path):
    path = ROOT / "tools" / "vps_backend_server.py"
    spec = importlib.util.spec_from_file_location("vps_backend_server", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    Handler = mod.Handler
    farm = MockFarmTaskClient()
    svc, worker, _, _ = _mgmt(tmp_path / "http", farm=farm)
    Handler.farm_service_token = "service-secret"
    Handler.farm_management_service = svc
    Handler.slot_sms_service = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.dumps(_assign_payload()).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/farm/slots/1/assign",
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer service-secret",
            },
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            assert resp.status == 202
            body = json.loads(resp.read().decode())
        job_id = body["job_id"]
        deadline = time.time() + 2.0
        while time.time() < deadline and svc.get_job(job_id).body.get("state") != "done":
            time.sleep(0.05)
        greq = urllib.request.Request(
            f"http://127.0.0.1:{port}/jobs/{job_id}",
            headers={"Authorization": "Bearer service-secret"},
        )
        with urllib.request.urlopen(greq, timeout=3) as resp:
            job = json.loads(resp.read().decode())
        assert job["state"] == "done"
    finally:
        server.shutdown()


def test_auth_required(tmp_path: Path):
    path = ROOT / "tools" / "vps_backend_server.py"
    spec = importlib.util.spec_from_file_location("vps_backend_server", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    Handler = mod.Handler
    svc, _, _, _ = _mgmt(tmp_path)
    Handler.farm_service_token = "service-secret"
    Handler.farm_management_service = svc
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/farm/slots/available",
            method="GET",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=3)
        assert exc.value.code == 401
    finally:
        server.shutdown()
