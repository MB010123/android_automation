from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from application.provisioning_service import ProvisioningService, ProvisioningServiceConfig
from domain.models import ActivationJob, ProvisioningResult
from domain.ports import ActivationJobSource, ActivationPayloadResolver, SubscriptionProvisioner


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
            return ProvisioningResult(True, job.job_id, job.slot_id, device_code=0)
        finally:
            with self._lock:
                self._active_slots.remove(job.slot_id)


class FakePayloadResolver(ActivationPayloadResolver):
    def resolve(self, job: ActivationJob) -> ActivationJob:
        return job


def make_service(source: FakeSource, provisioner: FakeProvisioner) -> ProvisioningService:
    return ProvisioningService(
        config=ProvisioningServiceConfig(max_workers=20),
        source=source,
        provisioner=provisioner,
        payload_resolver=FakePayloadResolver(),
        slot_map={1: "SERIAL-1", 2: "SERIAL-2"},
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
