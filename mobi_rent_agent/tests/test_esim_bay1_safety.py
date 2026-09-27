"""Bay-1 eSIM safety: authoritative assign, QR SSRF, install states, recovery."""
from __future__ import annotations

import json
import sys
import threading
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.farm_agent_tasks import FarmTaskRequest, execute_farm_task
from application.farm_task_executor import FarmTaskExecutorDeps
from application.install_state import (
    INSTALL_ACCEPTED,
    INSTALL_FAILED,
    INSTALL_VERIFICATION_UNKNOWN,
    INSTALL_VERIFIED,
)
from application.vps_api_contract import job_response_body
from application.vps_farm_management_service import VpsFarmManagementService
from application.vps_job_worker import VpsJobWorker
from domain.esim_capabilities import AndroidAuthorizationSnapshot
from domain.models import ActivationJob, ProvisioningResult
from domain.provisioning_state import ActivationVerdict
from domain.slot_isolation import SlotIsolationPolicy
from domain.verification import (
    ConnectivityLayer,
    FourLayerVerification,
    SettingsLpaLayer,
    SubscriptionLayer,
    TelephonyLayer,
)
from infrastructure.android_authorization_probe import StaticAuthorizationProbe
from infrastructure.authorized_esim_provider import AuthorizedEsimProvider
from infrastructure.esim_qr_security import esim_fetch_url_is_public_https, esim_fetch_url_is_safe
from infrastructure.farm_task_client import FarmTaskResponse
from infrastructure.slot_assignment_store import SlotAssignmentStore
from infrastructure.slot_event_store import SlotEventStore
from infrastructure.slot_public_id import public_id_for_farm_slot
from infrastructure.vps_job_store import VpsJobStore
from infrastructure.vps_rate_limiter import VpsRateLimiter
from tests.fakes_supabase import MemoryTenant, seed_owned_slot
from tests.test_farm_agent_tasks import FakeResolver, FakeRunner, _config

PREFIXES = ("https://files.loanerphones.com/", "https://example.test/")
OWNER = "11111111-1111-4111-8111-111111111111"
LOVABLE_QR = "https://files.loanerphones.com/slots/rental/qr.png"
SECRET = "LPA:1$sm-v4-prod-external.prod.ondemandconnectivity.com$UNIT-TEST-NOT-REAL"


class SyncWorker(VpsJobWorker):
    def enqueue_process(self, job_id: str) -> None:
        return None


class FarmSpy:
    def __init__(self, response: FarmTaskResponse | None = None) -> None:
        self.calls: list[dict] = []
        self.lookups: list[str] = []
        self.lookup_response: FarmTaskResponse | None = None
        self.response = response or FarmTaskResponse(
            ok=True,
            http_status=200,
            body={"ok": True, "install_state": INSTALL_ACCEPTED, "activation_code_sent": True},
        )

    def run_task(self, **kwargs: object) -> FarmTaskResponse:
        self.calls.append(dict(kwargs))
        return self.response

    def lookup_job(self, job_id: str) -> FarmTaskResponse | None:
        self.lookups.append(job_id)
        return self.lookup_response


def _layers(*, confirmed: bool = False, partial: bool = False) -> FourLayerVerification:
    cellular = confirmed
    embedded = confirmed or partial
    return FourLayerVerification(
        settings_lpa=SettingsLpaLayer(euicc_enabled=True, settings_profile_visible=True),
        subscription=SubscriptionLayer(
            subscription_present=embedded,
            subscription_embedded=embedded,
            default_data_sub_id=12 if embedded else -1,
            embedded_list_empty=not embedded,
        ),
        telephony=TelephonyLayer(data_registered=cellular, emergency_only=not cellular),
        connectivity=ConnectivityLayer(
            wifi_enabled=not cellular,
            cellular_transport_available=cellular,
            cellular_ip_present=cellular,
            default_route_cellular=cellular,
            internet_reachable=cellular,
            internet_proves_cellular=cellular,
        ),
        observation_complete=True,
    )


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


def _seed(tenant: MemoryTenant, rental_id: str, *, bay: int = 1, **kw: object) -> str:
    seed_owned_slot(
        tenant,
        bay=bay,
        rental_id=rental_id,
        user_id=OWNER,
        qr_code_url=str(kw.get("qr_code_url") or LOVABLE_QR),
        carrier_name=str(kw.get("carrier_name") or "Verizon"),
        imei2=str(kw.get("imei2") or "353456789012345"),
    )
    return rental_id


def _assign_body(rental_id: str, **kw: object) -> dict:
    body = {
        "rental_id": rental_id,
        "esim_qr_url": "https://evil.example/client-qr",
        "carrier": "client-carrier",
        "user_id": OWNER,
    }
    body.update(kw)
    return body


def test_rental_id_must_resolve_to_lovable_slot(tmp_path: Path):
    svc, *_rest = _harness(tmp_path)
    result = svc.assign_slot(1, _assign_body(str(uuid.uuid4())))
    assert result.http_status == 404
    assert result.body["error"] == "slot_not_found"


def test_rental_id_bay_mismatch_rejected(tmp_path: Path):
    svc, _w, _f, jobs, assign, tenant, _now = _harness(tmp_path)
    rental = _seed(tenant, str(uuid.uuid4()), bay=2)
    result = svc.assign_slot(1, _assign_body(rental))
    assert result.http_status == 404
    assert result.body["error"] == "slot_not_found"
    assert list(jobs._conn.execute("SELECT job_id FROM vps_jobs")) == []
    assert assign.is_assigned(1) is False


def test_client_qr_cannot_override_lovable_qr(tmp_path: Path):
    svc, worker, farm, jobs, _a, tenant, _now = _harness(tmp_path)
    rental = _seed(tenant, str(uuid.uuid4()))
    result = svc.assign_slot(1, _assign_body(rental))
    assert result.http_status == 202
    worker.process_job(result.body["job_id"])
    record = jobs.get(result.body["job_id"])
    assert record is not None
    assert record.request_payload["esim_qr_url"] == LOVABLE_QR
    assert "evil.example" not in json.dumps(record.request_payload)
    assert farm.calls[0]["payload"]["esim_qr_url"] == LOVABLE_QR


def test_client_carrier_cannot_override_lovable_carrier(tmp_path: Path):
    svc, worker, farm, jobs, _a, tenant, _now = _harness(tmp_path)
    rental = _seed(tenant, str(uuid.uuid4()), carrier_name="T-Mobile")
    result = svc.assign_slot(1, _assign_body(rental, carrier="AT&T"))
    assert result.http_status == 202
    worker.process_job(result.body["job_id"])
    payload = jobs.get(result.body["job_id"]).request_payload
    assert payload["carrier"] == "T-Mobile"
    assert payload["carrier_name"] == "T-Mobile"
    assert farm.calls[0]["payload"]["carrier"] == "T-Mobile"


def test_client_imei2_cannot_override_lovable_imei2(tmp_path: Path):
    svc, worker, _f, jobs, _a, tenant, _now = _harness(tmp_path)
    rental = _seed(tenant, str(uuid.uuid4()), imei2="353456789012345")
    rejected = svc.assign_slot(1, _assign_body(rental, imei2="000000000000000"))
    assert rejected.http_status == 400
    accepted = svc.assign_slot(1, _assign_body(rental))
    assert accepted.http_status == 202
    worker.process_job(accepted.body["job_id"])
    payload = jobs.get(accepted.body["job_id"]).request_payload
    assert payload["imei2"] == "353456789012345"
    assert "000000000000000" not in json.dumps(payload)


def test_arbitrary_qr_host_rejected(tmp_path: Path):
    assert esim_fetch_url_is_safe("https://evil.example/qr.png", allowed_url_prefixes=PREFIXES) is False
    svc, _w, _f, jobs, assign, tenant, _now = _harness(tmp_path)
    rental = _seed(tenant, str(uuid.uuid4()), qr_code_url="https://evil.example/qr.png")
    result = svc.assign_slot(1, _assign_body(rental))
    assert result.http_status == 400
    assert list(jobs._conn.execute("SELECT job_id FROM vps_jobs")) == []
    assert assign.is_assigned(1) is False


def test_private_and_loopback_qr_host_rejected():
    assert esim_fetch_url_is_public_https("https://127.0.0.1/qr.png") is False
    assert esim_fetch_url_is_public_https("https://localhost/qr.png") is False
    assert esim_fetch_url_is_public_https("https://10.0.0.8/qr.png") is False
    assert esim_fetch_url_is_public_https("https://192.168.1.9/qr.png") is False
    assert esim_fetch_url_is_public_https("https://169.254.169.254/latest") is False
    assert esim_fetch_url_is_public_https("https://metadata.google.internal/") is False
    assert esim_fetch_url_is_safe("https://127.0.0.1/qr.png", allowed_url_prefixes=PREFIXES) is False


def test_private_loopback_qr_rejected_on_assign(tmp_path: Path):
    svc, _w, _f, jobs, assign, tenant, _now = _harness(tmp_path)
    rental = _seed(tenant, str(uuid.uuid4()), qr_code_url="https://127.0.0.1/qr.png")
    result = svc.assign_slot(1, _assign_body(rental))
    assert result.http_status == 400
    assert assign.is_assigned(1) is False


def test_https_allowlisted_qr_works(tmp_path: Path):
    assert esim_fetch_url_is_safe(LOVABLE_QR, allowed_url_prefixes=PREFIXES) is True
    svc, worker, farm, _jobs, assign, tenant, _now = _harness(tmp_path)
    rental = _seed(tenant, str(uuid.uuid4()))
    result = svc.assign_slot(1, _assign_body(rental))
    assert result.http_status == 202
    worker.process_job(result.body["job_id"])
    assert farm.calls[0]["payload"]["esim_qr_url"] == LOVABLE_QR
    assert assign.is_assigned(1) is True


def test_activation_code_never_returned_by_get_jobs(tmp_path: Path):
    svc, worker, _f, jobs, _a, tenant, _now = _harness(tmp_path)
    rental = _seed(tenant, str(uuid.uuid4()))
    result = svc.assign_slot(1, _assign_body(rental))
    jobs.update(
        result.body["job_id"],
        result_payload={"install_state": INSTALL_ACCEPTED, "activation_code": SECRET},
    )
    body = svc.get_job(result.body["job_id"]).body
    dumped = json.dumps(body)
    assert "activation_code" not in dumped
    assert SECRET not in dumped
    assert "LPA:" not in dumped
    record = jobs.get(result.body["job_id"])
    public = job_response_body(record)
    assert "activation_code" not in json.dumps(public)


def test_activation_code_not_persisted_in_vps_sqlite(tmp_path: Path):
    svc, worker, _f, jobs, _a, tenant, _now = _harness(tmp_path)
    rental = _seed(tenant, str(uuid.uuid4()))
    result = svc.assign_slot(1, _assign_body(rental, activation_code=SECRET))
    jobs.create(
        job_type="assign",
        farm_slot_id=2,
        request_payload={"activation_code": SECRET, "rental_id": rental},
    )
    worker.process_job(result.body["job_id"])
    raw = (tmp_path / "jobs.sqlite").read_bytes()
    assert SECRET.encode() not in raw
    assert b"LPA:1$" not in raw


def test_successful_euicc_download_is_install_accepted_not_failed():
    provider = AuthorizedEsimProvider(
        StaticAuthorizationProbe(AndroidAuthorizationSnapshot(euicc_enabled=True, device_owner=True)),
        SlotIsolationPolicy({1}),
        live_download_armed=True,
        download_transport=lambda serial, job: {"success": True, "device_code": 0},
        verification_source=lambda serial, job: _layers(partial=True),
    )
    result = provider.provision("SERIAL-1", ActivationJob("job-1", 1, SECRET))
    assert result.install_state == INSTALL_ACCEPTED
    assert result.install_state != INSTALL_FAILED
    assert result.activation_code_sent is True
    assert result.success is False

    provisioner = MagicMock()
    provisioner.provision.return_value = ProvisioningResult(
        success=False,
        job_id="job-1",
        slot_id=1,
        verdict=ActivationVerdict.ACTIVATION_PARTIAL,
        install_state=INSTALL_ACCEPTED,
        activation_code_sent=True,
        error="download accepted; cellular not confirmed",
    )
    farm_result = execute_farm_task(
        adb_path="adb",
        slot_map={1: "SERIAL-A"},
        request=FarmTaskRequest(
            job_id="job-1",
            task_type="assign",
            farm_slot_id=1,
            payload={"esim_qr_url": "https://example.com/qr.png"},
        ),
        agent_config=_config(),
        deps=FarmTaskExecutorDeps(
            command_runner=FakeRunner(),
            provisioner=provisioner,
            payload_resolver=FakeResolver(),
        ),
    )
    assert farm_result.ok is True
    assert farm_result.http_status == 200
    assert farm_result.install_state == INSTALL_ACCEPTED
    assert farm_result.error is None


def test_verified_cellular_state_produces_install_verified():
    provider = AuthorizedEsimProvider(
        StaticAuthorizationProbe(AndroidAuthorizationSnapshot(euicc_enabled=True, device_owner=True)),
        SlotIsolationPolicy({1}),
        live_download_armed=True,
        download_transport=lambda serial, job: {"success": True, "device_code": 0},
        verification_source=lambda serial, job: _layers(confirmed=True),
    )
    result = provider.provision("SERIAL-1", ActivationJob("job-1", 1, SECRET))
    assert result.success is True
    assert result.verdict is ActivationVerdict.ACTIVATION_CONFIRMED
    assert result.install_state == INSTALL_VERIFIED
    assert result.activation_code_sent is True


def test_verification_unknown_does_not_release_the_bay(tmp_path: Path):
    farm = FarmSpy(
        FarmTaskResponse(
            ok=True,
            http_status=200,
            body={
                "ok": True,
                "install_state": INSTALL_VERIFICATION_UNKNOWN,
                "activation_code_sent": True,
            },
        )
    )
    svc, worker, _f, _jobs, assign, tenant, _now = _harness(tmp_path, farm=farm)
    rental = _seed(tenant, str(uuid.uuid4()))
    result = svc.assign_slot(1, _assign_body(rental))
    worker.process_job(result.body["job_id"])
    job = svc.get_job(result.body["job_id"]).body
    assert job["state"] == "done"
    assert job["install_state"] == INSTALL_VERIFICATION_UNKNOWN
    assert assign.is_assigned(1) is True


def test_activation_code_sent_failure_does_not_redownload(tmp_path: Path):
    farm = FarmSpy(
        FarmTaskResponse(
            ok=False,
            http_status=422,
            body={
                "error": "provisioning_failed",
                "install_state": INSTALL_FAILED,
                "activation_code_sent": True,
            },
            error="provisioning_failed",
        )
    )
    svc, worker, _f, _jobs, assign, tenant, _now = _harness(tmp_path, farm=farm)
    rental = _seed(tenant, str(uuid.uuid4()))
    result = svc.assign_slot(1, _assign_body(rental))
    job_id = result.body["job_id"]
    worker.process_job(job_id)
    job = svc.get_job(job_id).body
    assert job["state"] == "done"
    assert job["install_state"] == INSTALL_VERIFICATION_UNKNOWN
    assert assign.is_assigned(1) is True
    worker.process_job(job_id)
    worker.process_job(job_id)
    assert len(farm.calls) == 1


def test_lovable_recording_after_successful_installation(tmp_path: Path):
    svc, worker, _f, _jobs, _a, tenant, _now = _harness(tmp_path)
    rental = _seed(tenant, str(uuid.uuid4()))
    result = svc.assign_slot(1, _assign_body(rental))
    worker.process_job(result.body["job_id"])
    assert len(tenant.esims) == 1
    row = tenant.esims[0]
    assert row["user_id"] == OWNER
    assert row["farm_slot_id"] == 1
    assert row["qr_code_url"] == LOVABLE_QR
    assert row["rental_id"] == rental
    assert row["carrier"] == "Verizon"
    assert row["job_id"] == result.body["job_id"]
    assert SECRET not in json.dumps(row)


def test_lovable_recording_retry_is_idempotent(tmp_path: Path):
    svc, worker, _f, jobs, _a, tenant, _now = _harness(tmp_path)
    rental = _seed(tenant, str(uuid.uuid4()))
    result = svc.assign_slot(1, _assign_body(rental))
    record = jobs.get(result.body["job_id"])
    assert worker._record_esim_if_needed(record, INSTALL_ACCEPTED) is True
    assert worker._record_esim_if_needed(record, INSTALL_ACCEPTED) is True
    assert len(tenant.esims) == 1


def test_lovable_recording_failure_does_not_release_the_bay(tmp_path: Path):
    class BoomTenant(MemoryTenant):
        def record_esim_upload(self, **kwargs):
            raise OSError("lovable_unavailable")

    tenant = BoomTenant()
    farm = FarmSpy()
    svc, worker, _f, _jobs, assign, _t, _now = _harness(tmp_path, farm=farm, tenant=tenant)
    rental = _seed(tenant, str(uuid.uuid4()))
    result = svc.assign_slot(1, _assign_body(rental))
    worker.process_job(result.body["job_id"])
    job = svc.get_job(result.body["job_id"]).body
    assert job["state"] == "done"
    assert job["install_state"] == INSTALL_ACCEPTED
    assert job["tenant_record_error"] is True
    assert assign.is_assigned(1) is True


def test_running_job_recovery_does_not_blindly_redownload(tmp_path: Path):
    farm = FarmSpy()
    svc, worker, _f, jobs, assign, tenant, now = _harness(tmp_path, farm=farm)
    rental = _seed(tenant, str(uuid.uuid4()))
    result = svc.assign_slot(1, _assign_body(rental))
    job_id = result.body["job_id"]
    jobs.update(job_id, status="running", started_at=now[0], progress=10)
    worker.process_job(job_id)
    assert farm.calls == []
    assert jobs.get(job_id).status == "running"
    now[0] = 1_000.0 + 179.0
    worker.recover_running_job(job_id)
    assert jobs.get(job_id).status == "running"
    now[0] = 1_000.0 + 180.0
    worker.recover_running_job(job_id)
    job = svc.get_job(job_id).body
    assert job["state"] == "done"
    assert job["install_state"] == INSTALL_VERIFICATION_UNKNOWN
    assert job["activation_code_sent"] is True
    assert farm.calls == []
    assert assign.is_assigned(1) is True


def test_existing_farm_service_authentication_remains_required(tmp_path: Path):
    import importlib.util

    path = ROOT / "tools" / "vps_backend_server.py"
    spec = importlib.util.spec_from_file_location("vps_backend_bay1_auth", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    Handler = mod.Handler
    svc, *_rest = _harness(tmp_path)
    Handler.farm_service_token = "service-secret"
    Handler.farm_management_service = svc
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/farm/slots/available",
            method="GET",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=3)
        assert exc.value.code == 401
        authed = urllib.request.Request(
            f"http://127.0.0.1:{port}/farm/slots/available",
            method="GET",
            headers={"Authorization": "Bearer service-secret"},
        )
        with urllib.request.urlopen(authed, timeout=3) as resp:
            assert resp.status == 200
    finally:
        server.shutdown()


def test_existing_non_esim_functionality_remains_unchanged(tmp_path: Path):
    svc, worker, farm, _jobs, assign, _tenant, _now = _harness(tmp_path)
    reboot = svc.enqueue_action(public_id_for_farm_slot(1), "reboot", {})
    assert reboot.http_status == 202
    worker.process_job(reboot.body["job_id"])
    job = svc.get_job(reboot.body["job_id"]).body
    assert job["state"] == "done"
    assert farm.calls[0]["task_type"] == "reboot"
    assert assign.is_assigned(1) is False
    available = svc.list_available_slots()
    assert available.http_status == 200
    assert {item["bay"] for item in available.body["available"]} == {1, 2}
