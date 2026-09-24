"""Slot status API, heartbeat poller, and slot state machine (mocked Farm; no devices)."""
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

from application.farm_heartbeat_poller import FarmHeartbeatPoller
from application.vps_api_contract import failure_class, provisioning_phase
from application.vps_farm_management_service import VpsFarmManagementService
from application.vps_job_worker import VpsJobWorker
from application.vps_lovable_routes import parse_route
from application.vps_slot_state import ALLOWED_TRANSITIONS, can_transition, derive_slot_state
from infrastructure.farm_task_client import FarmTaskResponse
from infrastructure.slot_assignment_store import SlotAssignmentStore
from infrastructure.slot_event_store import SlotEventStore
from infrastructure.slot_public_id import public_id_for_farm_slot
from infrastructure.slot_status_store import SlotStatusStore
from infrastructure.vps_job_store import VpsJobRecord, VpsJobStore
from infrastructure.vps_rate_limiter import VpsRateLimiter

SLOT1 = public_id_for_farm_slot(1)
SLOT2 = public_id_for_farm_slot(2)
INTERVAL = 30.0


class FarmStub:
    def __init__(self) -> None:
        self.offline: list[int] = []
        self.ok = True
        self.raise_exc = False

    def __call__(self) -> dict:
        if self.raise_exc:
            raise ConnectionError("farm down")
        return {"ok": self.ok, "offline_slots": list(self.offline), "slot_count": 20, "adb_online": 20}


class MockFarmTaskClient:
    def __init__(self) -> None:
        self.mode = "ok"
        self.calls: list[dict] = []

    def run_task(self, **kwargs: object) -> FarmTaskResponse:
        self.calls.append(dict(kwargs))
        if self.mode == "ok":
            return FarmTaskResponse(ok=True, http_status=200, body={"ok": True})
        if self.mode == "manual":
            return FarmTaskResponse(
                ok=False,
                http_status=422,
                body={"error": "provisioning_failed", "message": "human Settings/LPA required"},
                error="provisioning_failed",
            )
        if self.mode == "unsupported":
            return FarmTaskResponse(
                ok=False, http_status=501, body={"error": "action_not_supported"}, error="action_not_supported"
            )
        return FarmTaskResponse(ok=False, http_status=503, body={}, error="farm_unreachable")


class SyncWorker(VpsJobWorker):
    """Test-only: do not spawn a thread on enqueue; tests call process_job explicitly."""

    def enqueue_process(self, job_id: str) -> None:  # noqa: D401
        return None


class Fixture:
    def __init__(self, tmp_path: Path) -> None:
        self.now = 1_000_000.0
        self.farm = FarmStub()
        self.task_client = MockFarmTaskClient()
        self.jobs = VpsJobStore(tmp_path / "jobs.sqlite")
        self.assign = SlotAssignmentStore(tmp_path / "assign.sqlite")
        self.events = SlotEventStore(tmp_path / "events.sqlite")
        self.status = SlotStatusStore(tmp_path / "status.sqlite")
        self.poller = FarmHeartbeatPoller(
            farm_status_fetcher=self.farm,
            status_store=self.status,
            event_store=self.events,
            known_farm_slots={1, 2},
            interval_seconds=INTERVAL,
        )
        self.worker = SyncWorker(
            job_store=self.jobs,
            assignment_store=self.assign,
            event_store=self.events,
            farm_task_client=self.task_client,
            poll_interval_seconds=3600.0,
        )
        self.svc = VpsFarmManagementService(
            job_store=self.jobs,
            assignment_store=self.assign,
            event_store=self.events,
            job_worker=self.worker,
            farm_status_fetcher=self.farm,
            known_farm_slots={1, 2},
            rate_limiter=VpsRateLimiter(per_slot_limit=100, global_limit=1000),
            status_store=self.status,
            heartbeat_interval_seconds=INTERVAL,
            clock=lambda: self.now,
        )

    def beat(self) -> None:
        self.poller.poll_once(now=self.now)

    def status_of(self, slot: str = SLOT1) -> dict:
        return self.svc.get_slot_status(slot).body


def _payload() -> dict:
    return {
        "rental_id": str(uuid.uuid4()),
        "esim_qr_url": "https://example.test/qr.png",
        "carrier": "test-carrier",
    }


# --- status derivation -------------------------------------------------------


def test_status_unknown_without_heartbeat(tmp_path: Path):
    fx = Fixture(tmp_path)
    body = fx.status_of()
    assert body["ok"] is True
    assert body["status"] == "unknown"
    assert body["heartbeat"] == "none"
    assert body["adb_online"] is None
    assert body["cellular_status"] == "unknown"
    assert body["imei2"] is None
    assert body["imei2_status"] == "unknown"
    assert "serial" not in json.dumps(body).lower()


def test_status_available_when_online_and_unassigned(tmp_path: Path):
    fx = Fixture(tmp_path)
    fx.beat()
    body = fx.status_of()
    assert body["status"] == "available"
    assert body["adb_online"] is True
    assert body["heartbeat"] == "fresh"
    assert body["last_seen_at"] is not None
    assert body["bay"] == 1
    assert body["slot_id"] == SLOT1


def test_status_offline_when_adb_unreachable(tmp_path: Path):
    fx = Fixture(tmp_path)
    fx.farm.offline = [1]
    fx.beat()
    body = fx.status_of()
    assert body["status"] == "offline"
    assert body["adb_online"] is False
    assert body["last_seen_at"] is None


def test_status_stale_heartbeat_becomes_unknown(tmp_path: Path):
    fx = Fixture(tmp_path)
    fx.beat()
    assert fx.status_of()["status"] == "available"
    fx.now += INTERVAL * 3 + 1
    body = fx.status_of()
    assert body["status"] == "unknown"
    assert body["heartbeat"] == "stale"
    assert body["adb_online"] is None


def test_status_farm_unreachable_marks_heartbeat(tmp_path: Path):
    fx = Fixture(tmp_path)
    fx.beat()
    fx.farm.raise_exc = True
    fx.beat()
    body = fx.status_of()
    assert body["heartbeat"] == "farm_unreachable"
    assert body["status"] == "unknown"
    # last known good sighting is preserved
    assert body["last_seen_at"] is not None


def test_status_provisioning_then_online(tmp_path: Path):
    fx = Fixture(tmp_path)
    fx.beat()
    result = fx.svc.assign_slot(1, _payload())
    assert result.http_status == 202
    job_id = result.body["job_id"]
    # worker not run yet -> pending assign job
    body = fx.status_of()
    assert body["status"] == "provisioning"
    assert body["active_job_id"] == job_id
    assert body["provisioning_phase"] == "queued"
    fx.worker.process_job(job_id)
    body = fx.status_of()
    assert body["status"] == "online"
    assert body["provisioning_phase"] == "completed"
    assert body["assigned"] is True
    assert body["rental_id"] is not None


def test_status_requires_manual_action(tmp_path: Path):
    fx = Fixture(tmp_path)
    fx.beat()
    fx.task_client.mode = "manual"
    result = fx.svc.assign_slot(1, _payload())
    fx.worker.process_job(result.body["job_id"])
    job = fx.svc.get_job(result.body["job_id"]).body
    assert job["state"] == "failed"
    assert job["provisioning_phase"] == "requires_manual_action"
    assert job["failure_class"] == "requires_manual_action"
    # bay released on failure -> status reflects manual step but no assignment
    body = fx.status_of()
    assert body["assigned"] is False
    assert body["provisioning_phase"] == "requires_manual_action"
    assert body["status"] in ("failed", "available")


def test_status_failed_after_permanent_failure(tmp_path: Path):
    fx = Fixture(tmp_path)
    fx.beat()
    fx.task_client.mode = "down"
    result = fx.svc.assign_slot(1, _payload())
    fx.worker.process_job(result.body["job_id"])
    job = fx.svc.get_job(result.body["job_id"]).body
    assert job["failure_class"] == "temporary"
    assert job["provisioning_phase"] == "failed"
    assert fx.status_of()["status"] == "failed"


def test_status_unsupported_phase(tmp_path: Path):
    fx = Fixture(tmp_path)
    fx.beat()
    fx.task_client.mode = "unsupported"
    result = fx.svc.assign_slot(1, _payload())
    fx.worker.process_job(result.body["job_id"])
    job = fx.svc.get_job(result.body["job_id"]).body
    assert job["ok"] is True  # envelope only; outcome is in state/phase
    assert job["state"] == "failed"
    assert job["provisioning_phase"] == "unsupported"
    assert job["failure_class"] == "unsupported"
    assert job["error"] == "action_not_supported"
    # Slot status: terminal failed state, phase stays `unsupported` (not collapsed to `failed`).
    body = fx.status_of()
    assert body["status"] == "failed"
    assert body["provisioning_phase"] == "unsupported"
    assert body["assigned"] is False
    # Deterministic: repeated evaluation yields identical derivation.
    assert fx.status_of()["status"] == "failed"
    kinds = [e.event_type for e in fx.events.list_events(1)]
    assert "provisioning_failed" in kinds and "provisioning_completed" not in kinds


def test_status_busy_during_reboot(tmp_path: Path):
    fx = Fixture(tmp_path)
    fx.beat()
    result = fx.svc.enqueue_action(SLOT1, "reboot", {})
    assert result.http_status == 202
    body = fx.status_of()
    assert body["status"] == "busy"
    assert body["active_job_type"] == "reboot"
    fx.worker.process_job(result.body["job_id"])
    assert fx.status_of()["status"] == "available"
    types = [e.event_type for e in fx.events.list_events(1)]
    assert "reboot_requested" in types
    assert "reboot_completed" in types


def test_status_unknown_slot_404(tmp_path: Path):
    fx = Fixture(tmp_path)
    assert fx.svc.get_slot_status(public_id_for_farm_slot(9)).http_status == 404
    assert fx.svc.get_slot_status("not-a-uuid").http_status == 404


def test_status_isolated_per_bay(tmp_path: Path):
    fx = Fixture(tmp_path)
    fx.farm.offline = [2]
    fx.beat()
    assert fx.status_of(SLOT1)["status"] == "available"
    assert fx.status_of(SLOT2)["status"] == "offline"


# --- heartbeat poller ---------------------------------------------------------


def test_heartbeat_emits_transition_events_only(tmp_path: Path):
    fx = Fixture(tmp_path)
    fx.beat()
    fx.beat()
    assert not [e for e in fx.events.list_events(1) if e.event_type.startswith("device_")]
    fx.farm.offline = [1]
    fx.beat()
    fx.beat()
    fx.farm.offline = []
    fx.beat()
    kinds = [e.event_type for e in fx.events.list_events(1)]
    assert kinds.count("device_offline") == 1
    assert kinds.count("device_online") == 1


def test_heartbeat_farm_not_ok_records_error(tmp_path: Path):
    fx = Fixture(tmp_path)
    fx.farm.ok = False
    out = fx.poller.poll_once(now=fx.now)
    assert out["ok"] is False
    hb = fx.status.get(1)
    assert hb is not None and hb.farm_ok is False


def test_status_store_preserves_last_seen(tmp_path: Path):
    store = SlotStatusStore(tmp_path / "s.sqlite")
    store.record(1, adb_online=True, farm_ok=True, now=100.0)
    hb = store.record(1, adb_online=False, farm_ok=True, now=200.0)
    assert hb.last_seen_at == 100.0
    assert hb.last_checked_at == 200.0
    # schema init idempotent on reopen
    again = SlotStatusStore(tmp_path / "s.sqlite")
    assert again.get(1) is not None


def test_heartbeat_poller_start_stop_is_bounded_and_idempotent(tmp_path: Path):
    fx = Fixture(tmp_path)
    fx.poller.start()
    fx.poller.start()  # second start must not spawn a duplicate thread
    assert fx.poller.is_running
    names = [t.name for t in threading.enumerate() if t.name == "vps-farm-heartbeat"]
    assert len(names) == 1
    started = time.monotonic()
    fx.poller.stop(join_timeout=2.0)
    assert time.monotonic() - started < 2.0
    assert not fx.poller.is_running
    # Stores are still open here (stop-before-close ordering); closing now must not raise.
    fx.status.close()
    fx.events.close()


def test_job_worker_stop_joins_bounded(tmp_path: Path):
    fx = Fixture(tmp_path)
    fx.worker._poll_interval = 0.05
    fx.worker.start()
    started = time.monotonic()
    fx.worker.stop(join_timeout=2.0)
    assert time.monotonic() - started < 2.0
    assert not (fx.worker._thread and fx.worker._thread.is_alive())


# --- state machine ------------------------------------------------------------


def test_state_machine_transitions():
    assert can_transition("available", "assigned")
    assert can_transition("assigned", "provisioning")
    assert can_transition("provisioning", "online")
    assert can_transition("provisioning", "failed")
    assert can_transition("provisioning", "requires_manual_action")
    assert can_transition("online", "offline")
    assert can_transition("offline", "online")
    assert can_transition("online", "busy")
    assert not can_transition("available", "online")
    assert not can_transition("failed", "online")
    assert not can_transition("offline", "provisioning")
    for state, targets in ALLOWED_TRANSITIONS.items():
        assert state not in targets


def test_derive_priority_rules():
    assert derive_slot_state(
        is_assigned=True, active_job_type="assign", last_assign_phase=None, heartbeat_fresh=False, adb_online=None
    ) == "provisioning"
    assert derive_slot_state(
        is_assigned=True, active_job_type="reboot", last_assign_phase="completed", heartbeat_fresh=True, adb_online=True
    ) == "busy"
    assert derive_slot_state(
        is_assigned=True, active_job_type=None, last_assign_phase="completed", heartbeat_fresh=False, adb_online=True
    ) == "unknown"
    assert derive_slot_state(
        is_assigned=True, active_job_type=None, last_assign_phase="completed", heartbeat_fresh=True, adb_online=False
    ) == "offline"
    assert derive_slot_state(
        is_assigned=True, active_job_type=None, last_assign_phase="completed", heartbeat_fresh=True, adb_online=True
    ) == "online"
    assert derive_slot_state(
        is_assigned=False, active_job_type=None, last_assign_phase=None, heartbeat_fresh=True, adb_online=True
    ) == "available"


@pytest.mark.parametrize(
    "phase,expected",
    [
        ("failed", "failed"),
        ("unsupported", "failed"),
        ("requires_manual_action", "available"),
        ("completed", "available"),
        (None, "available"),
    ],
)
def test_derive_unassigned_online_by_last_phase(phase, expected):
    kwargs = dict(is_assigned=False, active_job_type=None, heartbeat_fresh=True, adb_online=True)
    first = derive_slot_state(last_assign_phase=phase, **kwargs)
    assert first == expected
    assert derive_slot_state(last_assign_phase=phase, **kwargs) == first
    # An unassigned bay can never be reported online, whatever the last phase.
    assert first != "online"


# --- provisioning phase / failure classes -------------------------------------


def _job(status: str, error: str | None, message: str | None = None) -> VpsJobRecord:
    now = time.time()
    return VpsJobRecord(
        job_id="j",
        type="assign",
        farm_slot_id=1,
        status=status,
        progress=0,
        request_payload={},
        result_payload={"message": message} if message else None,
        error=error,
        idempotency_key=None,
        created_at=now,
        updated_at=now,
        started_at=None,
        completed_at=None,
    )


@pytest.mark.parametrize(
    "status,error,message,phase,klass",
    [
        ("pending", None, None, "queued", None),
        ("running", None, None, "provisioning", None),
        ("done", None, None, "completed", None),
        ("failed", "action_not_supported", None, "unsupported", "unsupported"),
        ("failed", "provisioning_failed", "human Settings/LPA required", "requires_manual_action", "requires_manual_action"),
        ("failed", "farm_unreachable", None, "failed", "temporary"),
        ("failed", "provisioning_failed", "device does not expose eUICC", "failed", "permanent"),
    ],
)
def test_provisioning_phase_matrix(status, error, message, phase, klass):
    record = _job(status, error, message)
    assert provisioning_phase(record) == phase
    assert failure_class(record) == klass


# --- routing + HTTP -----------------------------------------------------------


def test_route_parse_status():
    route = parse_route(f"/slots/{SLOT1}/status")
    assert route is not None and route.kind == "slot_status" and route.slot_public_id == SLOT1
    assert parse_route("/slots/not-a-uuid/status") is None


def test_http_status_requires_auth_and_returns_body(tmp_path: Path):
    path = ROOT / "tools" / "vps_backend_server.py"
    spec = importlib.util.spec_from_file_location("vps_backend_server_status", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    Handler = mod.Handler
    fx = Fixture(tmp_path)
    fx.beat()
    Handler.farm_service_token = "service-secret"
    Handler.farm_management_service = fx.svc
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{port}/slots/{SLOT1}/status"
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(urllib.request.Request(url), timeout=3)
        assert exc.value.code == 401
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(
                urllib.request.Request(url, headers={"Authorization": "Bearer wrong"}), timeout=3
            )
        assert exc.value.code == 401
        req = urllib.request.Request(url, headers={"Authorization": "Bearer service-secret"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            assert resp.status == 200
            body = json.loads(resp.read().decode())
        assert body["status"] == "available"
        assert body["slot_id"] == SLOT1
    finally:
        server.shutdown()
