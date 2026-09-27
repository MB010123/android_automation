"""Crash/timeout recovery for assign jobs: never blindly resend provision_esim."""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.farm_task_types import FarmTaskResult
from application.install_state import (
    INSTALL_ACCEPTED,
    INSTALL_FAILED,
    INSTALL_VERIFICATION_UNKNOWN,
    INSTALL_VERIFIED,
)
from application.vps_farm_management_service import VpsFarmManagementService
from application.vps_job_worker import VpsJobWorker
from infrastructure.farm_job_result_cache import (
    FarmJobResultCache,
    is_authoritative_pre_send_failure,
    should_reuse_assign_result,
)
from infrastructure.farm_task_client import FarmTaskClient, FarmTaskResponse
from infrastructure.slot_assignment_store import SlotAssignmentStore
from infrastructure.slot_event_store import SlotEventStore
from infrastructure.vps_job_store import VpsJobStore
from infrastructure.vps_rate_limiter import VpsRateLimiter
from tests.fakes_supabase import MemoryTenant, seed_owned_slot

PREFIXES = ("https://files.loanerphones.com/",)
OWNER = "11111111-1111-4111-8111-111111111111"
LOVABLE_QR = "https://files.loanerphones.com/slots/rental/qr.png"


class SyncWorker(VpsJobWorker):
    def enqueue_process(self, job_id: str) -> None:
        return None


class FarmSpy:
    def __init__(self) -> None:
        self.run_calls: list[dict] = []
        self.lookups: list[str] = []
        self.lookup_by_job: dict[str, FarmTaskResponse | None] = {}
        self.run_response = FarmTaskResponse(
            ok=True,
            http_status=200,
            body={"ok": True, "install_state": INSTALL_ACCEPTED, "activation_code_sent": True},
        )

    def run_task(self, **kwargs: object) -> FarmTaskResponse:
        self.run_calls.append(dict(kwargs))
        return self.run_response

    def lookup_job(self, job_id: str) -> FarmTaskResponse | None:
        self.lookups.append(job_id)
        if job_id in self.lookup_by_job:
            return self.lookup_by_job[job_id]
        return None


def _harness(tmp_path: Path, *, farm: FarmSpy | None = None, tenant: MemoryTenant | None = None):
    jobs = VpsJobStore(tmp_path / "jobs.sqlite")
    assign = SlotAssignmentStore(tmp_path / "assign.sqlite")
    events = SlotEventStore(tmp_path / "events.sqlite")
    tenant = tenant or MemoryTenant()
    farm = farm or FarmSpy()
    now = [1_000.0]
    worker = SyncWorker(
        job_store=jobs,
        assignment_store=assign,
        event_store=events,
        farm_task_client=farm,
        poll_interval_seconds=3600.0,
        auth_store=tenant,
        clock=lambda: now[0],
        running_stale_seconds=180.0,
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
    return svc, worker, farm, jobs, assign, tenant, now


def _seed_running_assign(tmp_path: Path, *, farm: FarmSpy | None = None, tenant: MemoryTenant | None = None):
    svc, worker, farm, jobs, assign, tenant, now = _harness(tmp_path, farm=farm, tenant=tenant)
    rental = str(uuid.uuid4())
    seed_owned_slot(
        tenant,
        bay=1,
        rental_id=rental,
        user_id=OWNER,
        qr_code_url=LOVABLE_QR,
        carrier_name="Verizon",
    )
    result = svc.assign_slot(
        1,
        {"rental_id": rental, "esim_qr_url": "https://evil.example/x", "carrier": "x", "user_id": OWNER},
    )
    assert result.http_status == 202
    job_id = result.body["job_id"]
    jobs.update(job_id, status="running", started_at=now[0], progress=10)
    now[0] = 1_000.0 + 180.0
    return svc, worker, farm, jobs, assign, tenant, now, job_id


def _cached(install_state: str, *, sent: bool, ok: bool = True) -> FarmTaskResponse:
    return FarmTaskResponse(
        ok=ok,
        http_status=200 if ok else 422,
        body={
            "ok": ok,
            "install_state": install_state,
            "activation_code_sent": sent,
            "found": True,
        },
        error=None if ok else "provisioning_failed",
    )


def test_stale_running_assign_does_not_call_run_task(tmp_path: Path):
    farm = FarmSpy()
    svc, worker, _f, _jobs, assign, _t, _n, job_id = _seed_running_assign(tmp_path, farm=farm)
    worker.recover_running_job(job_id)
    assert farm.run_calls == []
    assert farm.lookups == [job_id]
    job = svc.get_job(job_id).body
    assert job["install_state"] == INSTALL_VERIFICATION_UNKNOWN
    assert assign.is_assigned(1) is True


def test_stale_job_with_accepted_cache_recovers_as_accepted(tmp_path: Path):
    farm = FarmSpy()
    svc, worker, _f, _jobs, assign, tenant, _n, job_id = _seed_running_assign(tmp_path, farm=farm)
    farm.lookup_by_job[job_id] = _cached(INSTALL_ACCEPTED, sent=True)
    worker.recover_running_job(job_id)
    assert farm.run_calls == []
    job = svc.get_job(job_id).body
    assert job["state"] == "done"
    assert job["install_state"] == INSTALL_ACCEPTED
    assert assign.is_assigned(1) is True
    assert len(tenant.esims) == 1
    assert tenant.esims[0]["job_id"] == job_id


def test_stale_job_with_verified_cache_recovers_as_verified(tmp_path: Path):
    farm = FarmSpy()
    svc, worker, _f, _jobs, assign, tenant, _n, job_id = _seed_running_assign(tmp_path, farm=farm)
    farm.lookup_by_job[job_id] = _cached(INSTALL_VERIFIED, sent=True)
    worker.recover_running_job(job_id)
    assert farm.run_calls == []
    job = svc.get_job(job_id).body
    assert job["install_state"] == INSTALL_VERIFIED
    assert assign.is_assigned(1) is True
    assert len(tenant.esims) == 1


def test_stale_job_sent_but_unverified_becomes_unknown(tmp_path: Path):
    farm = FarmSpy()
    svc, worker, _f, _jobs, assign, tenant, _n, job_id = _seed_running_assign(tmp_path, farm=farm)
    farm.lookup_by_job[job_id] = _cached(INSTALL_VERIFICATION_UNKNOWN, sent=True)
    worker.recover_running_job(job_id)
    assert farm.run_calls == []
    job = svc.get_job(job_id).body
    assert job["install_state"] == INSTALL_VERIFICATION_UNKNOWN
    assert assign.is_assigned(1) is True
    assert tenant.esims == []


def test_stale_job_pre_send_failure_may_be_retried(tmp_path: Path):
    farm = FarmSpy()
    farm.run_response = FarmTaskResponse(
        ok=True,
        http_status=200,
        body={"ok": True, "install_state": INSTALL_ACCEPTED, "activation_code_sent": True},
    )
    svc, worker, _f, _jobs, assign, tenant, _n, job_id = _seed_running_assign(tmp_path, farm=farm)
    farm.lookup_by_job[job_id] = _cached(INSTALL_FAILED, sent=False, ok=False)
    worker.recover_running_job(job_id)
    assert len(farm.run_calls) == 1
    assert farm.run_calls[0]["job_id"] == job_id
    assert farm.run_calls[0]["task_type"] == "assign"
    job = svc.get_job(job_id).body
    assert job["install_state"] == INSTALL_ACCEPTED
    assert assign.is_assigned(1) is True
    assert len(tenant.esims) == 1


def test_recovery_never_sends_provision_esim_on_uncertain_or_cached_result(tmp_path: Path):
    farm = FarmSpy()
    _svc, worker, _f, _jobs, _a, _t, _n, job_id = _seed_running_assign(tmp_path, farm=farm)
    worker.recover_running_job(job_id)
    farm.lookup_by_job[job_id] = _cached(INSTALL_ACCEPTED, sent=True)
    # already done; a second recover must still not run_task
    worker.recover_running_job(job_id)
    assert farm.run_calls == []


def test_recovery_never_releases_bay_for_unknown(tmp_path: Path):
    farm = FarmSpy()
    svc, worker, _f, _jobs, assign, _t, _n, job_id = _seed_running_assign(tmp_path, farm=farm)
    worker.recover_running_job(job_id)
    assert svc.get_job(job_id).body["install_state"] == INSTALL_VERIFICATION_UNKNOWN
    assert assign.is_assigned(1) is True


def test_accepted_recovery_recording_is_idempotent(tmp_path: Path):
    farm = FarmSpy()
    svc, worker, _f, jobs, assign, tenant, _n, job_id = _seed_running_assign(tmp_path, farm=farm)
    farm.lookup_by_job[job_id] = _cached(INSTALL_ACCEPTED, sent=True)
    worker.recover_running_job(job_id)
    record = jobs.get(job_id)
    assert worker._record_esim_if_needed(record, INSTALL_ACCEPTED) is True
    assert worker._record_esim_if_needed(record, INSTALL_VERIFIED) is True
    assert len(tenant.esims) == 1
    assert assign.is_assigned(1) is True


def test_recovery_recording_failure_keeps_bay(tmp_path: Path):
    class BoomTenant(MemoryTenant):
        def record_esim_upload(self, **kwargs):
            raise OSError("lovable_unavailable")

    farm = FarmSpy()
    tenant = BoomTenant()
    svc, worker, _f, _jobs, assign, _t, _n, job_id = _seed_running_assign(
        tmp_path, farm=farm, tenant=tenant
    )
    farm.lookup_by_job[job_id] = _cached(INSTALL_VERIFIED, sent=True)
    worker.recover_running_job(job_id)
    job = svc.get_job(job_id).body
    assert job["state"] == "done"
    assert job["install_state"] == INSTALL_VERIFIED
    assert job["tenant_record_error"] is True
    assert assign.is_assigned(1) is True
    assert farm.run_calls == []


def test_repeated_recovery_does_not_create_second_install(tmp_path: Path):
    farm = FarmSpy()
    svc, worker, _f, _jobs, assign, tenant, now, job_id = _seed_running_assign(tmp_path, farm=farm)
    farm.lookup_by_job[job_id] = _cached(INSTALL_ACCEPTED, sent=True)
    worker.recover_running_job(job_id)
    now[0] += 180.0
    worker.recover_running_job(job_id)
    worker.process_job(job_id)
    assert farm.run_calls == []
    assert len(tenant.esims) == 1
    assert assign.is_assigned(1) is True
    assert svc.get_job(job_id).body["install_state"] == INSTALL_ACCEPTED


def test_cache_reuse_rules():
    assert should_reuse_assign_result({"install_state": INSTALL_ACCEPTED, "activation_code_sent": True})
    assert should_reuse_assign_result({"install_state": INSTALL_VERIFIED, "activation_code_sent": True})
    assert should_reuse_assign_result({"install_state": INSTALL_VERIFICATION_UNKNOWN, "activation_code_sent": True})
    assert should_reuse_assign_result({"install_state": INSTALL_FAILED, "activation_code_sent": True})
    assert is_authoritative_pre_send_failure({"install_state": INSTALL_FAILED, "activation_code_sent": False})
    assert should_reuse_assign_result({"install_state": INSTALL_FAILED, "activation_code_sent": False}) is False


def test_farm_cache_strips_activation_code(tmp_path: Path):
    cache = FarmJobResultCache(tmp_path / "farm_jobs.json")
    cache.put(
        "job-1",
        {
            "ok": True,
            "install_state": INSTALL_ACCEPTED,
            "activation_code_sent": True,
            "activation_code": "LPA:1$server$secret",
        },
    )
    row = cache.get("job-1")
    assert row is not None
    assert "activation_code" not in row
    raw = (tmp_path / "farm_jobs.json").read_text(encoding="utf-8")
    assert "LPA:1$" not in raw
    assert should_reuse_assign_result(row) is True


def test_farm_agent_job_query_is_read_only(tmp_path: Path):
    path = ROOT / "tools" / "farm_agent_status_server.py"
    spec = importlib.util.spec_from_file_location("farm_agent_recovery_server", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    Handler = mod.Handler
    cache = FarmJobResultCache()
    cache.put(
        "job-cached",
        {"ok": True, "install_state": INSTALL_ACCEPTED, "activation_code_sent": True},
    )
    Handler.api_token = "farm-agent-secret"
    Handler.job_cache = cache
    Handler.adb_path = "adb"
    Handler.slot_map = {}
    Handler.agent_config = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import urllib.error
        import urllib.request

        missing = urllib.request.Request(
            f"http://127.0.0.1:{port}/agent/jobs/missing",
            headers={"Authorization": "Bearer farm-agent-secret"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(missing, timeout=3)
        assert exc.value.code == 404

        found = urllib.request.Request(
            f"http://127.0.0.1:{port}/agent/jobs/job-cached",
            headers={"Authorization": "Bearer farm-agent-secret"},
        )
        with urllib.request.urlopen(found, timeout=3) as resp:
            body = json.loads(resp.read().decode())
        assert body["install_state"] == INSTALL_ACCEPTED
        assert body["activation_code_sent"] is True
        assert "activation_code" not in body

        unauth = urllib.request.Request(f"http://127.0.0.1:{port}/agent/jobs/job-cached")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(unauth, timeout=3)
        assert exc.value.code == 401
    finally:
        server.shutdown()
        Handler.job_cache = None
        Handler.api_token = None


def test_farm_task_client_lookup_does_not_post_run(tmp_path: Path):
    class Session:
        def __init__(self) -> None:
            self.posts: list[str] = []
            self.gets: list[str] = []

        def get(self, url, headers=None, timeout=None):
            self.gets.append(url)

            class Resp:
                status_code = 200
                content = b'{"ok":true,"install_state":"INSTALL_ACCEPTED","activation_code_sent":true}'

                def json(self):
                    return json.loads(self.content)

            return Resp()

        def post(self, url, json=None, headers=None, timeout=None):
            self.posts.append(url)
            raise AssertionError("lookup must not POST /agent/tasks/run")

    session = Session()
    client = FarmTaskClient("http://farm.test", "tok", session=session)
    found = client.lookup_job("job-1")
    assert found is not None
    assert found.body["install_state"] == INSTALL_ACCEPTED
    assert session.posts == []
    assert session.gets == ["http://farm.test/agent/jobs/job-1"]


def test_farm_assign_reuse_skips_second_execute():
    cache = FarmJobResultCache()
    first = FarmTaskResult(
        ok=True,
        http_status=200,
        install_state=INSTALL_ACCEPTED,
        activation_code_sent=True,
    )
    cache.remember_assign("job-1", first)
    assert should_reuse_assign_result(cache.get("job-1")) is True
    cache.remember_assign(
        "job-fail",
        FarmTaskResult(
            ok=False,
            http_status=422,
            error="device_offline",
            install_state=INSTALL_FAILED,
            activation_code_sent=False,
        ),
    )
    assert should_reuse_assign_result(cache.get("job-fail")) is False
    assert is_authoritative_pre_send_failure(cache.get("job-fail")) is True
