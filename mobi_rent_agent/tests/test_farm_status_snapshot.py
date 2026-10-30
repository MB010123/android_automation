"""Warm in-memory farm status snapshot (admin list <100ms)."""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infrastructure.farm_status_snapshot import FarmStatusSnapshot
from tools.farm_agent_status_server import Handler as FarmHandler


def test_snapshot_get_is_memory_after_warmup():
    calls = {"n": 0}

    def builder():
        calls["n"] += 1
        if calls["n"] > 1:
            time.sleep(0.25)
        return {"ok": True, "offline_slots": [], "mapped_slots": [1, 21]}

    snap = FarmStatusSnapshot(builder, interval_seconds=60)
    snap.start()
    try:
        t0 = time.perf_counter()
        body = snap.get()
        elapsed_ms = (time.perf_counter() - t0) * 1000
        assert body["ok"] is True
        assert body["mapped_slots"] == [1, 21]
        assert elapsed_ms < 100
        assert calls["n"] == 1
        again = snap.get()
        assert again["mapped_slots"] == [1, 21]
        assert calls["n"] == 1
    finally:
        snap.stop()


def test_snapshot_keeps_last_good_body_when_refresh_fails():
    state = {"n": 0}

    def builder():
        state["n"] += 1
        if state["n"] == 1:
            return {"ok": True, "offline_slots": [2]}
        raise RuntimeError("farm down")

    snap = FarmStatusSnapshot(builder, interval_seconds=60)
    snap.start()
    try:
        assert snap.get()["offline_slots"] == [2]
        snap.refresh_once()
        assert snap.get()["offline_slots"] == [2]
        assert snap.get()["ok"] is True
    finally:
        snap.stop()


def test_farm_health_http_serves_warm_snapshot():
    builds = {"n": 0}

    def builder():
        builds["n"] += 1
        time.sleep(0.05)
        return {
            "ok": True,
            "role": "farm",
            "mapped_slots": [21],
            "offline_slots": [],
            "adb_online": 1,
        }

    snap = FarmStatusSnapshot(builder, interval_seconds=60)
    snap.start()
    FarmHandler.api_token = "secret"
    FarmHandler.adb_path = "adb-missing-for-test"
    FarmHandler.slot_map = {21: "RFCNB056WEV"}
    FarmHandler.status_snapshot = snap
    server = ThreadingHTTPServer(("127.0.0.1", 0), FarmHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import urllib.request

        def call():
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/agent/health",
                headers={"Authorization": "Bearer secret"},
                method="GET",
            )
            t0 = time.perf_counter()
            with urllib.request.urlopen(req, timeout=3) as resp:
                body = json.loads(resp.read())
            return resp.status, body, (time.perf_counter() - t0) * 1000

        status, body, first_ms = call()
        assert status == 200 and body["mapped_slots"] == [21]
        status, body, second_ms = call()
        assert status == 200 and body["ok"] is True
        assert builds["n"] == 1
        assert second_ms < 100
        assert first_ms < 100
    finally:
        server.shutdown()
        snap.stop()
        FarmHandler.status_snapshot = None


def test_list_all_slots_snapshot_returns_under_50ms(tmp_path: Path):
    from application.vps_farm_management_service import VpsFarmManagementService
    from application.vps_job_worker import VpsJobWorker
    from infrastructure.slot_assignment_store import SlotAssignmentStore
    from infrastructure.slot_event_store import SlotEventStore
    from infrastructure.vps_job_store import VpsJobStore
    from infrastructure.vps_rate_limiter import VpsRateLimiter
    from tests.fakes_supabase import MemoryTenant

    class SlowTenant(MemoryTenant):
        def __init__(self) -> None:
            super().__init__()
            self.row_calls = 0

        def get_slot_row(self, farm_slot_id: int):
            self.row_calls += 1
            time.sleep(0.015)
            return super().get_slot_row(farm_slot_id)

    mapped = list(range(1, 22))
    tenant = SlowTenant()
    jobs = VpsJobStore(tmp_path / "jobs.sqlite")
    assign = SlotAssignmentStore(tmp_path / "assign.sqlite")
    events = SlotEventStore(tmp_path / "events.sqlite")
    worker = VpsJobWorker(
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
        event_store=events,
        job_worker=worker,
        farm_status_fetcher=lambda: {
            "ok": True,
            "offline_slots": [],
            "mapped_slots": mapped,
            "slot_count": len(mapped),
            "adb_online": len(mapped),
        },
        known_farm_slots=set(mapped),
        rate_limiter=VpsRateLimiter(per_slot_limit=100, global_limit=1000),
        auth_store=tenant,
    )
    svc.start_inventory_snapshot(interval_seconds=60)
    try:
        assert tenant.row_calls == len(mapped)
        t0 = time.perf_counter()
        listed = svc.list_all_slots()
        elapsed_ms = (time.perf_counter() - t0) * 1000
        assert listed.http_status == 200
        assert listed.body["count"] == 21
        assert elapsed_ms < 50
        calls_after = tenant.row_calls
        again = svc.list_all_slots()
        assert again.body["count"] == 21
        assert tenant.row_calls == calls_after
        from infrastructure.slot_public_id import public_id_for_farm_slot

        status = svc.get_slot_status(public_id_for_farm_slot(21))
        assert status.http_status == 200
        assert status.body["bay"] == 21
        assert tenant.row_calls == calls_after
    finally:
        svc.stop_inventory_snapshot()
