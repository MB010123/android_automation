"""Local Bay-1 assignment lifecycle: reservation, 409, and failed-job retry.

Uses in-memory tenant + mocked Farm. No production credentials, QR, or device I/O.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.install_state import INSTALL_ACCEPTED, INSTALL_FAILED, INSTALL_VERIFIED
from application.vps_farm_management_service import VpsFarmManagementService
from application.vps_job_worker import VpsJobWorker
from infrastructure.farm_task_client import FarmTaskResponse
from infrastructure.slot_assignment_store import SlotAssignmentStore
from infrastructure.slot_event_store import SlotEventStore
from infrastructure.vps_job_store import VpsJobStore
from infrastructure.vps_rate_limiter import VpsRateLimiter
from tests.fakes_supabase import MemoryTenant, seed_owned_slot

OWNER = "11111111-1111-4111-8111-111111111111"
PREFIXES = ("https://example.test/",)
FAKE_QR = "https://example.test/slots/rental/qr.png"


class SyncWorker(VpsJobWorker):
    def enqueue_process(self, job_id: str) -> None:
        return None


class MockFarm:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.response = FarmTaskResponse(
            ok=True,
            http_status=200,
            body={"ok": True, "install_state": INSTALL_ACCEPTED, "activation_code_sent": True},
        )

    def run_task(self, **kwargs: object) -> FarmTaskResponse:
        self.calls.append(dict(kwargs))
        return self.response


def _harness(tmp_path: Path, *, farm: MockFarm | None = None):
    jobs = VpsJobStore(tmp_path / "jobs.sqlite")
    assign = SlotAssignmentStore(tmp_path / "assign.sqlite")
    events = SlotEventStore(tmp_path / "events.sqlite")
    farm = farm or MockFarm()
    tenant = MemoryTenant()
    worker = SyncWorker(
        job_store=jobs,
        assignment_store=assign,
        event_store=events,
        farm_task_client=farm,
        poll_interval_seconds=3600.0,
        auth_store=tenant,
    )
    svc = VpsFarmManagementService(
        job_store=jobs,
        assignment_store=assign,
        event_store=events,
        job_worker=worker,
        farm_status_fetcher=lambda: {"ok": True, "offline_slots": [], "slot_count": 20, "adb_online": 20},
        known_farm_slots={1, 2},
        rate_limiter=VpsRateLimiter(per_slot_limit=100, global_limit=1000),
        auth_store=tenant,
        esim_url_prefixes=PREFIXES,
    )
    return svc, worker, farm, jobs, assign, tenant


def _payload(rental_id: str) -> dict:
    return {
        "rental_id": rental_id,
        "esim_qr_url": FAKE_QR,
        "carrier": "T-Mobile",
        "user_id": OWNER,
    }


def _seed(tenant: MemoryTenant, rental_id: str) -> str:
    seed_owned_slot(
        tenant,
        bay=1,
        rental_id=rental_id,
        user_id=OWNER,
        qr_code_url=FAKE_QR,
        carrier_name="T-Mobile",
    )
    return rental_id


def _available_bays(svc: VpsFarmManagementService) -> set[int]:
    return {item["bay"] for item in svc.list_available_slots().body["available"]}


def test_a_fresh_available_bay1_is_accepted(tmp_path: Path):
    svc, _w, farm, jobs, assign, tenant = _harness(tmp_path)
    rental = _seed(tenant, str(uuid.uuid4()))
    assert assign.is_assigned(1) is False
    assert jobs.get_active_for_slot(1) is None
    assert 1 in _available_bays(svc)
    result = svc.assign_slot(1, _payload(rental))
    assert result.http_status == 202
    assert result.body["bay"] == 1
    assert farm.calls == []
    assert assign.is_assigned(1) is True
    assert jobs.get(result.body["job_id"]).status == "pending"


def test_b_pending_job_reserves_bay_and_blocks_other_rental(tmp_path: Path):
    svc, _w, _f, jobs, assign, tenant = _harness(tmp_path)
    first = _seed(tenant, str(uuid.uuid4()))
    accepted = svc.assign_slot(1, _payload(first))
    assert accepted.http_status == 202
    assert assign.is_assigned(1) is True
    assert jobs.get(accepted.body["job_id"]).status == "pending"
    assert 1 not in _available_bays(svc)
    other = str(uuid.uuid4())
    _seed(tenant, other)
    blocked = svc.assign_slot(1, _payload(other))
    assert blocked.http_status == 409
    assert blocked.body["error"] == "slot_unavailable"
    assert jobs.get(accepted.body["job_id"]).status == "pending"


def test_c_terminal_success_releases_reservation(tmp_path: Path):
    farm = MockFarm()
    farm.response = FarmTaskResponse(
        ok=True,
        http_status=200,
        body={"ok": True, "install_state": INSTALL_VERIFIED, "activation_code_sent": True},
    )
    svc, worker, farm, jobs, assign, tenant = _harness(tmp_path, farm=farm)
    rental = _seed(tenant, str(uuid.uuid4()))
    first = svc.assign_slot(1, _payload(rental))
    worker.process_job(first.body["job_id"])
    record = jobs.get(first.body["job_id"])
    assert record.status == "done"
    assert record.result_payload["install_state"] == INSTALL_VERIFIED
    assert assign.is_assigned(1) is False
    assert 1 in _available_bays(svc)
    other = str(uuid.uuid4())
    _seed(tenant, other)
    second = svc.assign_slot(1, _payload(other))
    assert second.http_status == 202
    assert second.body["job_id"] != first.body["job_id"]


def test_d_terminal_failed_job_releases_reservation_and_keeps_history(tmp_path: Path):
    farm = MockFarm()
    farm.response = FarmTaskResponse(
        ok=False,
        http_status=404,
        body={"ok": False, "error": "not found", "install_state": INSTALL_FAILED},
        error="not found",
    )
    svc, worker, _f, jobs, assign, tenant = _harness(tmp_path, farm=farm)
    rental = _seed(tenant, str(uuid.uuid4()))
    first = svc.assign_slot(1, _payload(rental))
    job_id = first.body["job_id"]
    worker.process_job(job_id)
    record = jobs.get(job_id)
    assert record.status == "failed"
    assert record.result_payload["install_state"] == INSTALL_FAILED
    assert assign.is_assigned(1) is False
    assert jobs.get(job_id) is not None
    assert 1 in _available_bays(svc)


def test_e_failed_job_does_not_block_fresh_same_rental_assign(tmp_path: Path):
    farm = MockFarm()
    farm.response = FarmTaskResponse(
        ok=False,
        http_status=404,
        body={"ok": False, "error": "not found", "install_state": INSTALL_FAILED},
        error="not found",
    )
    svc, worker, farm, jobs, assign, tenant = _harness(tmp_path, farm=farm)
    rental = _seed(tenant, str(uuid.uuid4()))
    first = svc.assign_slot(1, _payload(rental))
    worker.process_job(first.body["job_id"])
    assert jobs.get(first.body["job_id"]).status == "failed"
    assert assign.is_assigned(1) is False
    farm.response = FarmTaskResponse(
        ok=True,
        http_status=200,
        body={"ok": True, "install_state": INSTALL_ACCEPTED, "activation_code_sent": True},
    )
    retry = svc.assign_slot(1, _payload(rental))
    assert retry.http_status == 202
    assert retry.body["job_id"] != first.body["job_id"]
    assert jobs.get(first.body["job_id"]).status == "failed"
    assert jobs.get(retry.body["job_id"]).status == "pending"
    assert assign.is_assigned(1) is True


def test_f_same_rental_pending_replay_returns_existing_job(tmp_path: Path):
    svc, _w, farm, jobs, assign, tenant = _harness(tmp_path)
    rental = _seed(tenant, str(uuid.uuid4()))
    first = svc.assign_slot(1, _payload(rental))
    second = svc.assign_slot(1, _payload(rental))
    assert first.http_status == 202
    assert second.http_status == 202
    assert first.body["job_id"] == second.body["job_id"]
    rows = list(jobs._conn.execute("SELECT job_id FROM vps_jobs"))
    assert len(rows) == 1
    assert farm.calls == []
    assert assign.is_assigned(1) is True


def test_g_pending_job_still_409_for_other_rental(tmp_path: Path):
    svc, worker, farm, jobs, assign, tenant = _harness(tmp_path)
    rental = _seed(tenant, str(uuid.uuid4()))
    first = svc.assign_slot(1, _payload(rental))
    jobs.update(first.body["job_id"], status="running", started_at=1.0, progress=10)
    other = str(uuid.uuid4())
    _seed(tenant, other)
    blocked = svc.assign_slot(1, _payload(other))
    assert blocked.http_status == 409
    assert blocked.body["error"] == "slot_unavailable"
    assert jobs.get(first.body["job_id"]).status == "running"
    assert farm.calls == []


def test_failed_history_survives_successful_retry(tmp_path: Path):
    farm = MockFarm()
    farm.response = FarmTaskResponse(
        ok=False,
        http_status=422,
        body={"ok": False, "error": "provisioning_failed", "install_state": INSTALL_FAILED},
        error="provisioning_failed",
    )
    svc, worker, farm, jobs, assign, tenant = _harness(tmp_path, farm=farm)
    rental = _seed(tenant, str(uuid.uuid4()))
    first = svc.assign_slot(1, _payload(rental))
    worker.process_job(first.body["job_id"])
    farm.response = FarmTaskResponse(
        ok=True,
        http_status=200,
        body={"ok": True, "install_state": INSTALL_ACCEPTED, "activation_code_sent": True},
    )
    retry = svc.assign_slot(1, _payload(rental))
    worker.process_job(retry.body["job_id"])
    assert jobs.get(first.body["job_id"]).status == "failed"
    assert jobs.get(retry.body["job_id"]).status == "done"
    assert assign.is_assigned(1) is False


def test_release_does_not_delete_a_newer_reservation(tmp_path: Path):
    assign = SlotAssignmentStore(tmp_path / "assign.sqlite")
    assert assign.claim(1, str(uuid.uuid4()), "job-old") is True
    assign.release(1, "job-old")
    assert assign.claim(1, str(uuid.uuid4()), "job-new") is True
    assign.release(1, "job-old")
    assert assign.is_assigned(1) is True
    held = assign.get(1)
    assert held is not None
    assert held.job_id == "job-new"
