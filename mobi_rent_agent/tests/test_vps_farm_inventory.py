"""Physical inventory from Farm health mapped_slots (no hardcoded 1–20)."""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.vps_farm_inventory import farm_agent_unavailable, mapped_farm_slots, offline_farm_slots
from application.vps_farm_management_service import VpsFarmManagementService
from application.vps_job_worker import VpsJobWorker
from infrastructure.slot_assignment_store import SlotAssignmentStore
from infrastructure.slot_event_store import SlotEventStore
from infrastructure.slot_public_id import public_id_for_farm_slot
from infrastructure.slot_reservation_store import SlotReservationStore
from infrastructure.vps_job_store import VpsJobStore
from infrastructure.vps_rate_limiter import VpsRateLimiter
from tests.fakes_supabase import MemoryTenant, seed_owned_slot
from tools.farm_agent_status_server import build_farm_status

OWNER = "11111111-1111-4111-8111-111111111111"
PREFIXES = ("https://example.test/",)
FAKE_QR = "https://example.test/slots/rental/qr.png"


class SyncWorker(VpsJobWorker):
    def enqueue_process(self, job_id: str) -> None:
        return None


def _health(*, mapped: list[int], offline: list[int] | None = None, error: str | None = None, ok: bool | None = None):
    offline = list(offline or [])
    body = {
        "ok": len(offline) == 0 and len(mapped) > 0 if ok is None else ok,
        "role": "farm",
        "mapped_slots": list(mapped),
        "offline_slots": offline,
        "slot_count": len(mapped),
        "adb_online": max(0, len(mapped) - len(offline)),
    }
    if error:
        body["error"] = error
        body["ok"] = False
    return body


def _svc(tmp_path: Path, *, health: dict):
    jobs = VpsJobStore(tmp_path / "jobs.sqlite")
    assign = SlotAssignmentStore(tmp_path / "assign.sqlite")
    reservations = SlotReservationStore(tmp_path / "slot_reservations.sqlite")
    events = SlotEventStore(tmp_path / "events.sqlite")
    tenant = MemoryTenant()
    worker = SyncWorker(
        job_store=jobs,
        assignment_store=assign,
        event_store=events,
        farm_task_client=None,
        poll_interval_seconds=3600.0,
        auth_store=tenant,
    )
    svc = VpsFarmManagementService(
        job_store=jobs,
        assignment_store=assign,
        reservation_store=reservations,
        event_store=events,
        job_worker=worker,
        farm_status_fetcher=lambda: health,
        known_farm_slots={1, 2, 3, 4, 20},
        rate_limiter=VpsRateLimiter(per_slot_limit=100, global_limit=1000),
        auth_store=tenant,
        esim_url_prefixes=PREFIXES,
    )
    return svc, reservations, tenant


def _bays(svc: VpsFarmManagementService) -> set[int]:
    result = svc.list_available_slots()
    assert result.http_status == 200
    return {item["bay"] for item in result.body["available"]}


def test_mapped_farm_slots_ignores_unknown_keys():
    assert mapped_farm_slots({"mapped_slots": [1, "2", "x"]}) == frozenset({1, 2})
    assert mapped_farm_slots({"offline_slots": [1]}) == frozenset()
    assert farm_agent_unavailable({"error": "adb_unavailable", "mapped_slots": [1]}) is True
    assert farm_agent_unavailable({"ok": False, "mapped_slots": [1, 2], "offline_slots": [2]}) is False
    assert offline_farm_slots({"offline_slots": [2]}) == frozenset({2})


def test_farm_health_reports_mapped_slots_from_slot_map():
    body = build_farm_status(adb_path="adb-missing-for-test", slot_map={1: "SER-A"})
    assert body["mapped_slots"] == [1]
    assert 2 not in body["mapped_slots"]


def test_farm_maps_only_slot_1_only_slot_1_is_inventory(tmp_path: Path):
    svc, _r, _t = _svc(tmp_path, health=_health(mapped=[1]))
    listed = svc.list_all_slots()
    assert listed.http_status == 200
    assert listed.body["count"] == 1
    assert listed.body["slots"][0]["bay"] == 1
    assert _bays(svc) == {1}


def test_unmapped_slot_2_is_never_available(tmp_path: Path):
    svc, _r, _t = _svc(tmp_path, health=_health(mapped=[1]))
    assert 2 not in _bays(svc)
    assert svc.get_slot_status(public_id_for_farm_slot(2)).http_status == 404


def test_mapped_adb_offline_slot_is_unavailable(tmp_path: Path):
    svc, _r, _t = _svc(tmp_path, health=_health(mapped=[1], offline=[1], ok=False))
    assert _bays(svc) == set()


def test_online_mapped_unreserved_slot_is_available(tmp_path: Path):
    svc, _r, _t = _svc(tmp_path, health=_health(mapped=[1]))
    assert _bays(svc) == {1}


def test_durably_reserved_slot_is_unavailable(tmp_path: Path):
    svc, _reservations, tenant = _svc(tmp_path, health=_health(mapped=[1]))
    rental = str(uuid.uuid4())
    seed_owned_slot(tenant, bay=1, rental_id=rental, user_id=OWNER, qr_code_url=FAKE_QR, carrier_name="T-Mobile")
    assert svc.reserve_slot(1, {"rental_id": rental, "user_id": OWNER}).http_status == 200
    assert _reservations.is_reserved(1)
    assert _bays(svc) == set()


def test_one_offline_slot_does_not_hide_unrelated_mapped_slots(tmp_path: Path):
    # Real Farm health sets ok=false when any mapped serial is not ADB "device".
    svc, _r, _t = _svc(tmp_path, health=_health(mapped=[1, 3], offline=[3], ok=False))
    assert farm_agent_unavailable(_health(mapped=[1, 3], offline=[3], ok=False)) is False
    assert _bays(svc) == {1}
    assert 3 not in _bays(svc)
    assert 2 not in _bays(svc)


def test_adb_unavailable_is_farm_unreachable(tmp_path: Path):
    svc, _r, _t = _svc(tmp_path, health=_health(mapped=[1], error="adb_unavailable"))
    result = svc.list_available_slots()
    assert result.http_status == 503
    assert result.body["error"] == "farm_unreachable"
