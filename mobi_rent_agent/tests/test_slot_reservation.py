"""Durable VPS slot reservation (mocked Farm; no live Android/GADS/QR)."""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.install_state import INSTALL_VERIFIED
from application.vps_farm_management_service import VpsFarmManagementService
from application.vps_job_worker import VpsJobWorker
from infrastructure.farm_task_client import FarmTaskResponse
from infrastructure.slot_assignment_store import SlotAssignmentStore
from infrastructure.slot_event_store import SlotEventStore
from infrastructure.slot_reservation_store import SlotReservationStore
from infrastructure.vps_job_store import VpsJobStore
from infrastructure.vps_rate_limiter import VpsRateLimiter
from tests.fakes_supabase import MemoryTenant, seed_owned_slot

OWNER_A = "11111111-1111-4111-8111-111111111111"
OWNER_B = "22222222-2222-4222-8222-222222222222"
PREFIXES = ("https://example.test/",)
FAKE_QR = "https://example.test/slots/rental/qr.png"


class SyncWorker(VpsJobWorker):
    def enqueue_process(self, job_id: str) -> None:
        return None


class MockFarm:
    def run_task(self, **kwargs: object) -> FarmTaskResponse:
        return FarmTaskResponse(
            ok=True,
            http_status=200,
            body={"ok": True, "install_state": INSTALL_VERIFIED, "activation_code_sent": True},
        )


def _harness(tmp_path: Path):
    jobs = VpsJobStore(tmp_path / "jobs.sqlite")
    assign = SlotAssignmentStore(tmp_path / "assign.sqlite")
    reservations = SlotReservationStore(tmp_path / "slot_reservations.sqlite")
    events = SlotEventStore(tmp_path / "events.sqlite")
    tenant = MemoryTenant()
    worker = SyncWorker(
        job_store=jobs,
        assignment_store=assign,
        event_store=events,
        farm_task_client=MockFarm(),
        poll_interval_seconds=3600.0,
        auth_store=tenant,
    )
    svc = VpsFarmManagementService(
        job_store=jobs,
        assignment_store=assign,
        reservation_store=reservations,
        event_store=events,
        job_worker=worker,
        farm_status_fetcher=lambda: {"ok": True, "offline_slots": [], "mapped_slots": [1, 2], "slot_count": 2, "adb_online": 2},
        known_farm_slots={1, 2},
        rate_limiter=VpsRateLimiter(per_slot_limit=100, global_limit=1000),
        auth_store=tenant,
        esim_url_prefixes=PREFIXES,
    )
    return svc, worker, jobs, assign, reservations, tenant


def _seed(tenant: MemoryTenant, *, bay: int, rental_id: str, user_id: str = OWNER_A) -> str:
    seed_owned_slot(
        tenant,
        bay=bay,
        rental_id=rental_id,
        user_id=user_id,
        qr_code_url=FAKE_QR,
        carrier_name="T-Mobile",
    )
    return rental_id


def _available(svc: VpsFarmManagementService) -> set[int]:
    return {item["bay"] for item in svc.list_available_slots().body["available"]}


def test_reserve_one_bay_successfully(tmp_path: Path):
    svc, _w, _j, assign, reservations, tenant = _harness(tmp_path)
    rental = _seed(tenant, bay=1, rental_id=str(uuid.uuid4()))
    result = svc.reserve_slot(1, {"rental_id": rental, "user_id": OWNER_A})
    assert result.http_status == 200
    assert result.body["ok"] is True
    assert result.body["reserved"] is True
    assert result.body["bay"] == 1
    assert result.body["rental_id"] == rental
    assert "serial" not in result.body
    assert reservations.is_reserved(1)
    assert assign.is_assigned(1) is False
    assert 1 not in _available(svc)
    assert 2 in _available(svc)
    assert tenant.owner_of_slot(1) == OWNER_A


def test_second_rental_cannot_reserve_same_bay(tmp_path: Path):
    svc, _w, _j, _a, reservations, tenant = _harness(tmp_path)
    first = _seed(tenant, bay=1, rental_id=str(uuid.uuid4()), user_id=OWNER_A)
    assert svc.reserve_slot(1, {"rental_id": first}).http_status == 200
    second = str(uuid.uuid4())
    tenant.ensure_profile(OWNER_B, "b@example.com")
    seed_owned_slot(
        tenant,
        bay=2,
        rental_id=second,
        user_id=OWNER_B,
        qr_code_url=FAKE_QR,
        carrier_name="T-Mobile",
    )
    blocked = svc.reserve_slot(1, {"rental_id": second, "user_id": OWNER_B})
    assert blocked.http_status == 409
    assert blocked.body["error"] == "slot_unavailable"
    held = reservations.get(1)
    assert held is not None
    assert held.rental_id == first


def test_reservation_survives_assign_job_completion(tmp_path: Path):
    svc, worker, jobs, assign, reservations, tenant = _harness(tmp_path)
    rental = _seed(tenant, bay=1, rental_id=str(uuid.uuid4()))
    assert svc.reserve_slot(1, {"rental_id": rental}).http_status == 200
    assigned = svc.assign_slot(
        1,
        {"rental_id": rental, "esim_qr_url": FAKE_QR, "carrier": "T-Mobile", "user_id": OWNER_A},
    )
    assert assigned.http_status == 202
    worker.process_job(assigned.body["job_id"])
    assert jobs.get(assigned.body["job_id"]).status == "done"
    assert assign.is_assigned(1) is False
    assert reservations.is_reserved(1)
    assert reservations.get(1).rental_id == rental
    assert 1 not in _available(svc)
    other = _seed(tenant, bay=1, rental_id=str(uuid.uuid4()), user_id=OWNER_A)
    assert svc.assign_slot(1, {"rental_id": other, "esim_qr_url": FAKE_QR, "carrier": "T-Mobile"}).http_status == 409
    assert svc.reserve_slot(1, {"rental_id": other}).http_status == 409


def test_reservation_survives_vps_restart(tmp_path: Path):
    db = tmp_path / "slot_reservations.sqlite"
    rental = str(uuid.uuid4())
    first = SlotReservationStore(db)
    assert first.claim(1, rental) is True
    first.close()
    second = SlotReservationStore(db)
    held = second.get(1)
    assert held is not None
    assert held.rental_id == rental
    assert second.is_reserved(1) is True
    second.close()


def test_different_bays_reserved_independently(tmp_path: Path):
    svc, _w, _j, _a, reservations, tenant = _harness(tmp_path)
    rental_1 = _seed(tenant, bay=1, rental_id=str(uuid.uuid4()), user_id=OWNER_A)
    rental_2 = str(uuid.uuid4())
    seed_owned_slot(tenant, bay=2, rental_id=rental_2, user_id=OWNER_B, qr_code_url=FAKE_QR)
    tenant.ensure_profile(OWNER_B, "b@example.com")
    assert svc.reserve_slot(1, {"rental_id": rental_1}).http_status == 200
    assert svc.reserve_slot(2, {"rental_id": rental_2, "user_id": OWNER_B}).http_status == 200
    assert reservations.get(1).rental_id == rental_1
    assert reservations.get(2).rental_id == rental_2
    assert _available(svc) == set()


def test_releasing_one_reservation_does_not_affect_another_bay(tmp_path: Path):
    svc, _w, _j, _a, reservations, tenant = _harness(tmp_path)
    rental_1 = _seed(tenant, bay=1, rental_id=str(uuid.uuid4()), user_id=OWNER_A)
    rental_2 = str(uuid.uuid4())
    tenant.ensure_profile(OWNER_B, "b@example.com")
    seed_owned_slot(tenant, bay=2, rental_id=rental_2, user_id=OWNER_B, qr_code_url=FAKE_QR)
    svc.reserve_slot(1, {"rental_id": rental_1})
    svc.reserve_slot(2, {"rental_id": rental_2, "user_id": OWNER_B})
    released = svc.release_reservation(1, rental_1)
    assert released.http_status == 200
    assert released.body["released"] is True
    assert reservations.is_reserved(1) is False
    assert reservations.get(2).rental_id == rental_2
    assert 1 in _available(svc)
    assert 2 not in _available(svc)
    steal = svc.release_reservation(2, rental_1)
    assert steal.http_status == 409
    assert reservations.is_reserved(2) is True


def test_ownership_checks_remain_intact(tmp_path: Path):
    svc, _w, jobs, assign, reservations, tenant = _harness(tmp_path)
    owner_rental = _seed(tenant, bay=1, rental_id=str(uuid.uuid4()), user_id=OWNER_A)
    other_rental = str(uuid.uuid4())
    tenant.ensure_profile(OWNER_B, "b@example.com")
    stolen = svc.reserve_slot(1, {"rental_id": other_rental, "user_id": OWNER_B})
    assert stolen.http_status == 404
    assert reservations.is_reserved(1) is False
    assert tenant.owner_of_slot(1) == OWNER_A
    wrong_user = svc.reserve_slot(1, {"rental_id": owner_rental, "user_id": OWNER_B})
    assert wrong_user.http_status == 409
    assert tenant.owner_of_slot(1) == OWNER_A
    assert list(jobs._conn.execute("SELECT job_id FROM vps_jobs")) == []
    assert assign.is_assigned(1) is False


def test_reserve_rejects_client_serial(tmp_path: Path):
    svc, _w, _j, _a, reservations, tenant = _harness(tmp_path)
    rental = _seed(tenant, bay=1, rental_id=str(uuid.uuid4()))
    for key in ("serial", "adb_serial", "device_serial", "device_id", "udid"):
        result = svc.reserve_slot(1, {"rental_id": rental, key: "should-not-be-used"})
        assert result.http_status == 400
        assert result.body["error"] == "invalid_request"
    assert reservations.is_reserved(1) is False
    assert 1 in _available(svc)


def test_same_rental_reserve_is_idempotent(tmp_path: Path):
    svc, _w, _j, _a, reservations, tenant = _harness(tmp_path)
    rental = _seed(tenant, bay=1, rental_id=str(uuid.uuid4()))
    first = svc.reserve_slot(1, {"rental_id": rental})
    second = svc.reserve_slot(1, {"rental_id": rental})
    assert first.http_status == 200
    assert second.http_status == 200
    assert reservations.get(1).rental_id == rental


def test_legacy_assign_creates_durable_reservation(tmp_path: Path):
    svc, worker, jobs, assign, reservations, tenant = _harness(tmp_path)
    rental = _seed(tenant, bay=1, rental_id=str(uuid.uuid4()))
    result = svc.assign_slot(
        1,
        {"rental_id": rental, "esim_qr_url": FAKE_QR, "carrier": "T-Mobile", "user_id": OWNER_A},
    )
    assert result.http_status == 202
    worker.process_job(result.body["job_id"])
    assert jobs.get(result.body["job_id"]).status == "done"
    assert assign.is_assigned(1) is False
    assert reservations.get(1).rental_id == rental
    assert 1 not in _available(svc)
