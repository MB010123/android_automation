"""Rental-end orchestration: GADS → operator cleanup → unclaim → reservation."""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.install_state import INSTALL_VERIFIED
from application.vps_farm_management_service import ApiResult, VpsFarmManagementService
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


class FakeGads:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.calls: list[tuple[int, str]] = []

    def release_device(self, slot_id: int, rental_id: str) -> ApiResult:
        self.calls.append((int(slot_id), str(rental_id)))
        return ApiResult(self.status, {"ok": True, "slot_id": int(slot_id), "released": True})


def _harness(tmp_path: Path, *, gads: FakeGads | None = None):
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
        farm_status_fetcher=lambda: {
            "ok": True,
            "offline_slots": [],
            "mapped_slots": [1, 2],
            "slot_count": 2,
            "adb_online": 2,
        },
        known_farm_slots={1, 2},
        rate_limiter=VpsRateLimiter(per_slot_limit=100, global_limit=1000),
        auth_store=tenant,
        esim_url_prefixes=PREFIXES,
        remote_access=gads if gads is not None else FakeGads(),
    )
    return svc, worker, jobs, assign, reservations, tenant, events, svc._remote_access


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


def _event_types(events: SlotEventStore, bay: int) -> list[str]:
    return [item.event_type for item in events.list_events(bay)]


def _assert_no_secrets(body: dict) -> None:
    raw = str(body)
    assert "serial" not in body
    assert "adb_serial" not in body
    assert "udid" not in body
    assert "FARM_SERVICE_TOKEN" not in raw
    assert "loanerphones" not in raw.lower()
    assert "18171FDF6005WG" not in raw


def test_normal_rental_end_releases_occupancy(tmp_path: Path):
    gads = FakeGads()
    svc, _w, _j, assign, reservations, tenant, events, _g = _harness(tmp_path, gads=gads)
    rental = _seed(tenant, bay=1, rental_id=str(uuid.uuid4()))
    assert svc.reserve_slot(1, {"rental_id": rental, "user_id": OWNER_A}).http_status == 200
    assert 1 not in _available(svc)

    result = svc.end_rental(rental, explicit=True)
    assert result.http_status == 200
    assert result.body["ok"] is True
    assert result.body["ended"] is True
    assert result.body["tenant_unclaimed"] is True
    assert result.body["reservation_released"] is True
    assert result.body["gads_released"] is True
    assert result.body["device_cleanup"] == "required"
    _assert_no_secrets(result.body)

    assert gads.calls == [(1, rental)]
    assert tenant.owner_of_slot(1) is None
    assert reservations.is_reserved(1) is False
    assert assign.is_assigned(1) is False
    assert 1 in _available(svc)
    kinds = _event_types(events, 1)
    assert kinds.index("rental_end_started") < kinds.index("device_cleanup_required")
    assert kinds.index("device_cleanup_required") < kinds.index("slot_unclaimed")
    assert kinds.index("slot_unclaimed") < kinds.index("slot_reservation_released")
    cleanup = [e for e in events.list_events(1) if e.event_type == "device_cleanup_required"]
    assert cleanup
    assert "no_factory_reset" in cleanup[0].detail
    assert "no_silent_esim_delete" in cleanup[0].detail
    assert "wipe" not in cleanup[0].detail.lower()


def test_expired_rental_ends_without_explicit_flag(tmp_path: Path):
    svc, _w, _j, _a, reservations, tenant, _e, _g = _harness(tmp_path)
    rental = _seed(tenant, bay=1, rental_id=str(uuid.uuid4()))
    assert svc.reserve_slot(1, {"rental_id": rental}).http_status == 200
    tenant.slots[1]["ends_at"] = svc._clock() - 30

    result = svc.end_rental(rental, explicit=False)
    assert result.http_status == 200
    assert tenant.owner_of_slot(1) is None
    assert reservations.is_reserved(1) is False
    assert 1 in _available(svc)


def test_gads_session_already_gone_is_idempotent(tmp_path: Path):
    gads = FakeGads(status=404)
    svc, _w, _j, _a, reservations, tenant, _e, _g = _harness(tmp_path, gads=gads)
    rental = _seed(tenant, bay=1, rental_id=str(uuid.uuid4()))
    assert svc.reserve_slot(1, {"rental_id": rental}).http_status == 200

    result = svc.end_rental(rental, explicit=True)
    assert result.http_status == 200
    assert gads.calls == [(1, rental)]
    assert reservations.is_reserved(1) is False
    assert tenant.owner_of_slot(1) is None


def test_tenant_unclaim_succeeds(tmp_path: Path):
    svc, _w, _j, _a, reservations, tenant, _e, _g = _harness(tmp_path)
    rental = _seed(tenant, bay=1, rental_id=str(uuid.uuid4()))
    svc.reserve_slot(1, {"rental_id": rental})
    assert tenant.unclaim_slot(1, rental) is True
    assert tenant.owner_of_slot(1) is None
    # Full orchestration still releases the leftover reservation.
    result = svc.end_rental(rental, explicit=True)
    assert result.http_status == 200
    assert reservations.is_reserved(1) is False


def test_tenant_unclaim_fails_keeps_reservation(tmp_path: Path):
    gads = FakeGads()
    svc, _w, _j, _a, reservations, tenant, events, _g = _harness(tmp_path, gads=gads)
    rental = _seed(tenant, bay=1, rental_id=str(uuid.uuid4()))
    svc.reserve_slot(1, {"rental_id": rental})
    tenant.unclaim_fails = True

    result = svc.end_rental(rental, explicit=True)
    assert result.http_status == 503
    assert result.body["error"] == "auth_unavailable"
    assert gads.calls == [(1, rental)]
    assert tenant.owner_of_slot(1) == OWNER_A
    assert reservations.is_reserved(1) is True
    assert 1 not in _available(svc)
    kinds = _event_types(events, 1)
    assert "device_cleanup_required" in kinds
    assert "slot_reservation_released" not in kinds

    tenant.unclaim_fails = False
    retry = svc.end_rental(rental, explicit=True)
    assert retry.http_status == 200
    assert reservations.is_reserved(1) is False
    assert tenant.owner_of_slot(1) is None


def test_reservation_release_is_idempotent(tmp_path: Path):
    svc, _w, _j, _a, reservations, tenant, _e, _g = _harness(tmp_path)
    rental = _seed(tenant, bay=1, rental_id=str(uuid.uuid4()))
    svc.reserve_slot(1, {"rental_id": rental})
    first = svc.end_rental(rental, explicit=True)
    second = svc.end_rental(rental, explicit=True)
    assert first.http_status == 200
    assert second.http_status == 200
    assert reservations.is_reserved(1) is False
    assert 1 in _available(svc)


def test_reservation_not_released_before_end_condition(tmp_path: Path):
    svc, worker, jobs, assign, reservations, tenant, _e, _g = _harness(tmp_path)
    rental = _seed(tenant, bay=1, rental_id=str(uuid.uuid4()))
    assigned = svc.assign_slot(
        1,
        {"rental_id": rental, "esim_qr_url": FAKE_QR, "carrier": "T-Mobile", "user_id": OWNER_A},
    )
    assert assigned.http_status == 202
    worker.process_job(assigned.body["job_id"])
    assert jobs.get(assigned.body["job_id"]).status == "done"
    assert assign.is_assigned(1) is False
    assert reservations.is_reserved(1) is True
    assert tenant.owner_of_slot(1) == OWNER_A

    tenant.slots[1]["ends_at"] = svc._clock() + 3600
    blocked = svc.end_rental(rental, explicit=False)
    assert blocked.http_status == 409
    assert blocked.body["error"] == "rental_active"
    assert reservations.is_reserved(1) is True
    assert tenant.owner_of_slot(1) == OWNER_A
    assert 1 not in _available(svc)


def test_second_customer_cannot_acquire_bay_before_release(tmp_path: Path):
    svc, _w, _j, _a, reservations, tenant, _e, _g = _harness(tmp_path)
    first = _seed(tenant, bay=1, rental_id=str(uuid.uuid4()), user_id=OWNER_A)
    assert svc.reserve_slot(1, {"rental_id": first}).http_status == 200
    second = str(uuid.uuid4())
    tenant.ensure_profile(OWNER_B, "b@example.com")
    seed_owned_slot(tenant, bay=2, rental_id=second, user_id=OWNER_B, qr_code_url=FAKE_QR)
    blocked = svc.reserve_slot(1, {"rental_id": second, "user_id": OWNER_B})
    assert blocked.http_status == 409
    assert blocked.body["error"] == "slot_unavailable"
    assert reservations.get(1).rental_id == first
    assert tenant.owner_of_slot(1) == OWNER_A


def test_bay_available_only_after_complete_release_sequence(tmp_path: Path):
    gads = FakeGads(status=502)
    svc, _w, _j, _a, reservations, tenant, events, _g = _harness(tmp_path, gads=gads)
    first = _seed(tenant, bay=1, rental_id=str(uuid.uuid4()), user_id=OWNER_A)
    svc.reserve_slot(1, {"rental_id": first})
    assert 1 not in _available(svc)

    gads_fail = svc.end_rental(first, explicit=True)
    assert gads_fail.http_status == 503
    assert reservations.is_reserved(1) is True
    assert tenant.owner_of_slot(1) == OWNER_A
    assert 1 not in _available(svc)
    assert "slot_unclaimed" not in _event_types(events, 1)

    gads.status = 200
    tenant.unclaim_fails = True
    unclaim_fail = svc.end_rental(first, explicit=True)
    assert unclaim_fail.http_status == 503
    assert 1 not in _available(svc)
    assert reservations.is_reserved(1) is True

    tenant.unclaim_fails = False
    done = svc.end_rental(first, explicit=True)
    assert done.http_status == 200
    assert 1 in _available(svc)

    second = str(uuid.uuid4())
    seed_owned_slot(tenant, bay=1, rental_id=second, user_id=OWNER_B, qr_code_url=FAKE_QR)
    tenant.ensure_profile(OWNER_B, "b@example.com")
    claimed = svc.reserve_slot(1, {"rental_id": second, "user_id": OWNER_B})
    assert claimed.http_status == 200
    assert reservations.get(1).rental_id == second


def test_vps_restart_preserves_active_rental_reservation(tmp_path: Path):
    svc, worker, jobs, assign, reservations, tenant, events, gads = _harness(tmp_path)
    rental = _seed(tenant, bay=1, rental_id=str(uuid.uuid4()))
    assert svc.reserve_slot(1, {"rental_id": rental}).http_status == 200
    db_path = reservations.db_path
    reservations.close()

    reopened = SlotReservationStore(db_path)
    assert reopened.get(1) is not None
    assert reopened.get(1).rental_id == rental

    restarted = VpsFarmManagementService(
        job_store=jobs,
        assignment_store=assign,
        reservation_store=reopened,
        event_store=events,
        job_worker=worker,
        farm_status_fetcher=lambda: {
            "ok": True,
            "offline_slots": [],
            "mapped_slots": [1, 2],
            "slot_count": 2,
            "adb_online": 2,
        },
        known_farm_slots={1, 2},
        rate_limiter=VpsRateLimiter(per_slot_limit=100, global_limit=1000),
        auth_store=tenant,
        esim_url_prefixes=PREFIXES,
        remote_access=gads,
    )
    tenant.slots[1]["ends_at"] = restarted._clock() + 7200
    still_active = restarted.end_rental(rental, explicit=False)
    assert still_active.http_status == 409
    assert still_active.body["error"] == "rental_active"
    assert reopened.is_reserved(1) is True
    assert tenant.owner_of_slot(1) == OWNER_A
    assert 1 not in _available(restarted)
    restarted.sweep_ended_rentals()
    assert reopened.is_reserved(1) is True


def test_sweep_releases_only_expired_rentals(tmp_path: Path):
    svc, _w, _j, _a, reservations, tenant, _e, _g = _harness(tmp_path)
    live = _seed(tenant, bay=1, rental_id=str(uuid.uuid4()), user_id=OWNER_A)
    expired = str(uuid.uuid4())
    tenant.ensure_profile(OWNER_B, "b@example.com")
    seed_owned_slot(tenant, bay=2, rental_id=expired, user_id=OWNER_B, qr_code_url=FAKE_QR)
    assert svc.reserve_slot(1, {"rental_id": live}).http_status == 200
    assert svc.reserve_slot(2, {"rental_id": expired, "user_id": OWNER_B}).http_status == 200
    tenant.slots[1]["ends_at"] = svc._clock() + 3600
    tenant.slots[2]["ends_at"] = svc._clock() - 10

    svc.sweep_ended_rentals()
    assert reservations.is_reserved(1) is True
    assert tenant.owner_of_slot(1) == OWNER_A
    assert reservations.is_reserved(2) is False
    assert tenant.owner_of_slot(2) is None
    assert 1 not in _available(svc)
    assert 2 in _available(svc)
