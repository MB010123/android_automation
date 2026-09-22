from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from application.provisioning_service import ProvisioningService, ProvisioningServiceConfig
from domain.models import ActivationJob, ProvisioningResult
from domain.ports import ActivationJobSource, ActivationPayloadResolver, SubscriptionProvisioner
from domain.provisioning_state import ActivationVerdict, JobState
from domain.slot_isolation import SlotIsolationPolicy
from domain.verification import (
    ConnectivityLayer,
    FourLayerVerification,
    SettingsLpaLayer,
    SubscriptionLayer,
    TelephonyLayer,
)
from infrastructure.human_activation_provider import HumanActivationProvider


class FakeSource(ActivationJobSource):
    def __init__(
        self,
        jobs: list[ActivationJob],
        fetch_error: Exception | None = None,
        report_failures: int = 0,
    ) -> None:
        self.jobs = jobs
        self.fetch_error = fetch_error
        self.report_failures = report_failures
        self.requested_slots: list[int] = []
        self.reported: list[ProvisioningResult] = []

    def fetch_pending(self, slot_ids):
        self.requested_slots = list(slot_ids)
        if self.fetch_error:
            raise self.fetch_error
        return list(self.jobs)

    def report_result(self, result: ProvisioningResult) -> None:
        if self.report_failures > 0:
            self.report_failures -= 1
            raise RuntimeError("backend unavailable")
        self.reported.append(result)


class FakeProvisioner(SubscriptionProvisioner):
    def __init__(self, failing_slots: set[int] | None = None) -> None:
        self.failing_slots = failing_slots or set()
        self.calls: list[tuple[str, ActivationJob]] = []
        self._active_slots: set[int] = set()
        self._lock = threading.Lock()

    def provision(self, serial: str, job: ActivationJob) -> ProvisioningResult:
        with self._lock:
            assert job.slot_id not in self._active_slots
            self._active_slots.add(job.slot_id)
        try:
            self.calls.append((serial, job))
            if job.slot_id in self.failing_slots:
                raise RuntimeError("simulated device failure")
            return ProvisioningResult.from_verdict(
                job.job_id,
                job.slot_id,
                ActivationVerdict.ACTIVATION_CONFIRMED,
                device_code=0,
            )
        finally:
            with self._lock:
                self._active_slots.remove(job.slot_id)


class FakePayloadResolver(ActivationPayloadResolver):
    def resolve(self, job: ActivationJob) -> ActivationJob:
        return job


def make_service(
    source: FakeSource,
    provisioner,
    isolation: SlotIsolationPolicy | None = None,
    slot_map: dict[int, str] | None = None,
    max_workers: int = 20,
    verification_source=None,
) -> ProvisioningService:
    return ProvisioningService(
        config=ProvisioningServiceConfig(max_workers=max_workers),
        source=source,
        provisioner=provisioner,
        payload_resolver=FakePayloadResolver(),
        slot_map=slot_map or {1: "SERIAL-1", 2: "SERIAL-2"},
        isolation=isolation or SlotIsolationPolicy({1, 2}),
        verification_source=verification_source,
    )


def _snapshot(*, verdict: str) -> FourLayerVerification:
    confirmed = verdict == "confirmed"
    partial = verdict == "partial"
    unknown = verdict == "unknown"
    return FourLayerVerification(
        settings_lpa=SettingsLpaLayer(
            euicc_enabled=True,
            settings_profile_visible=True if partial or confirmed else None,
        ),
        subscription=SubscriptionLayer(
            subscription_present=confirmed,
            subscription_embedded=confirmed,
            default_data_sub_id=12 if confirmed else -1,
            embedded_list_empty=not confirmed,
        ),
        telephony=TelephonyLayer(
            data_registered=confirmed,
            emergency_only=not confirmed,
        ),
        connectivity=ConnectivityLayer(
            wifi_enabled=False,
            cellular_transport_available=confirmed,
            cellular_ip_present=confirmed,
            default_route_cellular=confirmed,
            internet_reachable=confirmed,
            internet_proves_cellular=confirmed,
        ),
        observation_complete=not unknown,
        explicit_failure=verdict == "failed",
        failure_reason="conflict" if verdict == "failed" else None,
    )


def test_run_once_provisions_and_reports_each_slot():
    source = FakeSource(
        [
            ActivationJob("job-1", 1, "LPA:1$server$one"),
            ActivationJob("job-2", 2, "LPA:1$server$two"),
        ]
    )
    provisioner = FakeProvisioner()

    results = make_service(source, provisioner).run_once()

    assert len(results) == 2
    assert all(result.success for result in results)
    assert set(source.requested_slots) == {1, 2}
    assert {result.job_id for result in source.reported} == {"job-1", "job-2"}


def test_device_failure_is_isolated_to_its_slot():
    source = FakeSource(
        [
            ActivationJob("job-1", 1, "LPA:1$server$one"),
            ActivationJob("job-2", 2, "LPA:1$server$two"),
        ]
    )
    provisioner = FakeProvisioner(failing_slots={1})

    results = make_service(source, provisioner).run_once()

    by_slot = {result.slot_id: result for result in results}
    assert by_slot[1].success is False
    assert by_slot[2].success is True
    assert len(source.reported) == 2


def test_jobs_for_the_same_slot_are_processed_serially():
    source = FakeSource(
        [
            ActivationJob("job-1", 1, "LPA:1$server$one"),
            ActivationJob("job-2", 1, "LPA:1$server$two"),
        ]
    )
    provisioner = FakeProvisioner()

    results = make_service(source, provisioner).run_once()

    assert [result.job_id for result in results] == ["job-1", "job-2"]


def test_fetch_failure_does_not_escape_the_cycle():
    source = FakeSource([], fetch_error=RuntimeError("backend unavailable"))

    assert make_service(source, FakeProvisioner()).run_once() == []


def test_failed_result_report_is_retried_on_next_cycle():
    source = FakeSource(
        [ActivationJob("job-1", 1, "LPA:1$server$one")],
        report_failures=1,
    )
    service = make_service(source, FakeProvisioner())

    service.run_once()
    source.jobs = []
    service.run_once()

    assert [result.job_id for result in source.reported] == ["job-1"]


def test_claim_uses_isolation_not_full_map():
    production_map = {slot: f"SERIAL-{slot}" for slot in range(1, 21)}
    source = FakeSource([ActivationJob("job-1", 1, "LPA:1$server$one")])
    service = make_service(
        source,
        FakeProvisioner(),
        isolation=SlotIsolationPolicy({1}),
        slot_map=production_map,
    )

    service.run_once()

    assert source.requested_slots == [1]
    assert service.last_claim_ids == [1]
    assert 2 not in source.requested_slots


def test_off_allowlist_returned_job_is_rejected():
    source = FakeSource(
        [
            ActivationJob("job-1", 1, "LPA:1$server$one"),
            ActivationJob("job-2", 2, "LPA:1$server$two"),
        ]
    )
    provisioner = FakeProvisioner()
    service = make_service(source, provisioner, isolation=SlotIsolationPolicy({1}))

    results = service.run_once()

    by_slot = {result.slot_id: result for result in results}
    assert by_slot[2].success is False
    assert by_slot[2].verdict is not None
    assert by_slot[2].verdict.value == "ACTIVATION_FAILED"
    assert "allowlist" in (by_slot[2].error or "")
    assert [serial for serial, job in provisioner.calls] == ["SERIAL-1"]
    assert source.requested_slots == [1]


def test_empty_intersection_refuses_without_claiming():
    source = FakeSource([ActivationJob("job-9", 9, "LPA:1$server$nine")])
    service = make_service(
        source,
        FakeProvisioner(),
        isolation=SlotIsolationPolicy({1}),
        slot_map={9: "SERIAL-9"},
    )

    assert service.run_once() == []
    assert source.requested_slots == []
    assert service.last_claim_ids == []
    assert service.last_worker_count == 0


def test_human_provider_does_not_resolve_activation_payload():
    class RecordingResolver(FakePayloadResolver):
        def __init__(self) -> None:
            self.calls = 0

        def resolve(self, job):
            self.calls += 1
            raise AssertionError("human path must not resolve activation payloads")

    source = FakeSource([ActivationJob("job-1", 1, "LPA:1$server$secret")])
    resolver = RecordingResolver()
    service = ProvisioningService(
        config=ProvisioningServiceConfig(max_workers=20),
        source=source,
        provisioner=HumanActivationProvider(),
        payload_resolver=resolver,
        slot_map={1: "SERIAL-1"},
        isolation=SlotIsolationPolicy({1}),
    )

    results = service.run_once()

    assert resolver.calls == 0
    assert results[0].success is False
    assert "not sent" in (results[0].error or "")


def test_worker_count_is_bounded_by_claim_ids():
    production_map = {slot: f"SERIAL-{slot}" for slot in range(1, 21)}
    source = FakeSource([ActivationJob("job-1", 1, "LPA:1$server$one")])
    service = make_service(
        source,
        FakeProvisioner(),
        isolation=SlotIsolationPolicy({1}),
        slot_map=production_map,
        max_workers=20,
    )

    service.run_once()

    assert service.last_worker_count == 1


def test_human_path_stops_at_waiting_without_snapshot():
    source = FakeSource([ActivationJob("job-1", 1, "LPA:1$server$secret")])
    service = make_service(
        source,
        HumanActivationProvider(),
        isolation=SlotIsolationPolicy({1}),
        slot_map={1: "SERIAL-1"},
    )
    results = service.run_once()
    assert results[0].success is False
    assert "secret" not in str(results[0].to_dict())
    assert "LPA:" not in str(results[0].to_dict())
    assert service.job_state("job-1") is JobState.WAITING_FOR_ACTIVATION
    assert service.job_history("job-1") == [
        JobState.PENDING,
        JobState.CLAIMED,
        JobState.PREFLIGHT,
        JobState.WAITING_FOR_ACTIVATION,
    ]


def test_service_confirmed_reaches_active_and_success():
    source = FakeSource([ActivationJob("job-1", 1, qr_url="https://example.test/q.png")])
    service = make_service(
        source,
        HumanActivationProvider(),
        isolation=SlotIsolationPolicy({1}),
        slot_map={1: "SERIAL-1"},
        verification_source=lambda _job: _snapshot(verdict="confirmed"),
    )
    results = service.run_once()
    assert results[0].success is True
    assert results[0].verdict is ActivationVerdict.ACTIVATION_CONFIRMED
    assert service.job_state("job-1") is JobState.ACTIVE
    assert JobState.VERIFICATION in service.job_history("job-1")
    assert service.job_history("job-1")[-1] is JobState.ACTIVE


def test_service_partial_cannot_become_active():
    source = FakeSource([ActivationJob("job-1", 1, qr_url="https://example.test/q.png")])
    service = make_service(
        source,
        HumanActivationProvider(),
        isolation=SlotIsolationPolicy({1}),
        slot_map={1: "SERIAL-1"},
        verification_source=lambda _job: _snapshot(verdict="partial"),
    )
    results = service.run_once()
    assert results[0].success is False
    assert results[0].verdict is ActivationVerdict.ACTIVATION_PARTIAL
    assert service.job_state("job-1") is JobState.WAITING_FOR_ACTIVATION
    assert service.job_state("job-1") is not JobState.ACTIVE


def test_service_unknown_cannot_become_active():
    source = FakeSource([ActivationJob("job-1", 1, qr_url="https://example.test/q.png")])
    service = make_service(
        source,
        HumanActivationProvider(),
        isolation=SlotIsolationPolicy({1}),
        slot_map={1: "SERIAL-1"},
        verification_source=lambda _job: _snapshot(verdict="unknown"),
    )
    results = service.run_once()
    assert results[0].success is False
    assert results[0].verdict is ActivationVerdict.VERIFICATION_UNKNOWN
    assert service.job_state("job-1") is JobState.RETRYABLE
    assert service.job_state("job-1") is not JobState.ACTIVE


def test_service_failed_cannot_become_active():
    source = FakeSource([ActivationJob("job-1", 1, qr_url="https://example.test/q.png")])
    service = make_service(
        source,
        HumanActivationProvider(),
        isolation=SlotIsolationPolicy({1}),
        slot_map={1: "SERIAL-1"},
        verification_source=lambda _job: _snapshot(verdict="failed"),
    )
    results = service.run_once()
    assert results[0].success is False
    assert results[0].verdict is ActivationVerdict.ACTIVATION_FAILED
    assert service.job_state("job-1") is JobState.FAILED


def test_confirmed_job_is_not_activated_twice():
    class CountingHuman(HumanActivationProvider):
        def __init__(self) -> None:
            self.submits = 0

        def submit_activation(self, serial, job):
            self.submits += 1
            return super().submit_activation(serial, job)

    source = FakeSource([ActivationJob("job-1", 1, qr_url="https://example.test/q.png")])
    human = CountingHuman()
    service = make_service(
        source,
        human,
        isolation=SlotIsolationPolicy({1}),
        slot_map={1: "SERIAL-1"},
        verification_source=lambda _job: _snapshot(verdict="confirmed"),
    )
    first = service.run_once()
    second = service.run_once()
    assert first[0].success is True
    assert second[0].success is True
    assert human.submits == 1
    assert service.job_state("job-1") is JobState.ACTIVE


def test_off_allowlist_job_records_failed_preflight_states():
    source = FakeSource([ActivationJob("job-2", 2, "LPA:1$server$two")])
    service = make_service(
        source,
        HumanActivationProvider(),
        isolation=SlotIsolationPolicy({1}),
        slot_map={1: "SERIAL-1", 2: "SERIAL-2"},
    )
    results = service.run_once()
    assert results[0].success is False
    assert service.job_state("job-2") is JobState.FAILED
    assert service.job_history("job-2") == [
        JobState.PENDING,
        JobState.CLAIMED,
        JobState.PREFLIGHT,
        JobState.FAILED,
    ]
