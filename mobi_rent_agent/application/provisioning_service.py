"""Application use case for isolated eSIM job orchestration.

Human/inert path only: the service applies the approved job state machine
in memory. It does not persist states to a database or invent new backend
APIs. Claim/result HTTP still uses the existing ActivationJobSource
contract and only runs if a caller constructs and starts this service
(the daemon does not, while PROVISIONING_ENDPOINT is unset).
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from domain.models import ActivationJob, ProvisioningResult
from domain.ports import ActivationJobSource, ActivationPayloadResolver, SubscriptionProvisioner
from domain.provisioning_state import (
    ActivationVerdict,
    JobState,
    apply_transition,
    is_terminal,
)
from domain.slot_isolation import SlotIsolationError, SlotIsolationPolicy
from domain.verification import FourLayerVerification
from application.slot_coordinator import SlotOperationCoordinator

logger = logging.getLogger("mobi_rent_agent.provisioning")

VerificationSource = Callable[[ActivationJob], FourLayerVerification | None]


@dataclass
class JobRecord:
    """In-memory job progress. Not written to disk or backend."""

    job_id: str
    slot_id: int
    state: JobState = JobState.PENDING
    history: list[JobState] = field(default_factory=lambda: [JobState.PENDING])
    verdict: ActivationVerdict | None = None

    def advance(self, destination: JobState) -> None:
        self.state = apply_transition(self.state, destination)
        self.history.append(self.state)


@dataclass(frozen=True)
class ProvisioningServiceConfig:
    poll_interval_seconds: float = 10.0
    max_workers: int = 20

    def __post_init__(self) -> None:
        if self.poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be greater than zero")
        if not (1 <= self.max_workers <= 20):
            raise ValueError("max_workers must be between 1 and 20")


class ProvisioningService:
    """Polls activation work and advances isolated human-path jobs."""

    def __init__(
        self,
        config: ProvisioningServiceConfig,
        source: ActivationJobSource,
        provisioner: SubscriptionProvisioner,
        payload_resolver: ActivationPayloadResolver,
        slot_map: dict[int, str],
        coordinator: SlotOperationCoordinator | None = None,
        isolation: SlotIsolationPolicy | None = None,
        verification_source: VerificationSource | None = None,
    ) -> None:
        self._config = config
        self._source = source
        self._provisioner = provisioner
        self._payload_resolver = payload_resolver
        self._slot_map = dict(slot_map)
        self._isolation = isolation or SlotIsolationPolicy()
        self._verification_source = verification_source
        self._coordinator = coordinator or SlotOperationCoordinator(list(slot_map))
        self.last_claim_ids: list[int] = []
        self.last_worker_count: int = 0
        self._records: dict[str, JobRecord] = {}
        self._results: dict[str, ProvisioningResult] = {}
        self._record_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._cycle_lock = threading.Lock()
        self._pending_reports: dict[str, ProvisioningResult] = {}
        self._report_lock = threading.Lock()

    def job_state(self, job_id: str) -> JobState | None:
        record = self._records.get(job_id)
        return None if record is None else record.state

    def job_history(self, job_id: str) -> list[JobState]:
        record = self._records.get(job_id)
        return [] if record is None else list(record.history)

    def run_forever(self) -> None:
        logger.info(
            "Provisioning service starting (interval=%.1fs, workers=%d)",
            self._config.poll_interval_seconds,
            self._config.max_workers,
        )
        while not self._stop_event.is_set():
            self.run_once()
            self._stop_event.wait(self._config.poll_interval_seconds)

    def stop(self) -> None:
        self._stop_event.set()

    def run_once(self) -> list[ProvisioningResult]:
        """Process one queue snapshot without allowing overlapping cycles."""
        if not self._cycle_lock.acquire(blocking=False):
            logger.warning("Skipping overlapping provisioning cycle")
            return []

        try:
            return self._run_cycle()
        finally:
            self._cycle_lock.release()

    def _run_cycle(self) -> list[ProvisioningResult]:
        self._flush_pending_reports()
        try:
            claim_ids = self._isolation.refuse_if_empty(self._slot_map)
        except SlotIsolationError as exc:
            logger.error("Refusing provisioning cycle: %s", exc)
            self.last_claim_ids = []
            self.last_worker_count = 0
            return []

        self.last_claim_ids = list(claim_ids)
        try:
            jobs = list(self._source.fetch_pending(claim_ids))
        except Exception as exc:
            logger.exception("Failed to fetch activation jobs: %s", exc)
            return []

        jobs_by_slot: dict[int, list[ActivationJob]] = defaultdict(list)
        results: list[ProvisioningResult] = []
        allowed = set(claim_ids)
        for job in jobs:
            if job.slot_id not in allowed or job.slot_id not in self._slot_map:
                result = self._fail_preflight(
                    job,
                    (
                        f"slot {job.slot_id} is outside the provisioning allowlist "
                        f"{sorted(self._isolation.allowed_slot_ids)}"
                        if job.slot_id not in allowed
                        else "slot is not managed by this agent"
                    ),
                )
                results.append(result)
                self._report_safely(result)
                continue
            jobs_by_slot[job.slot_id].append(job)

        if not jobs_by_slot:
            self.last_worker_count = 0
            return results

        worker_count = min(
            self._isolation.max_workers(self._config.max_workers, self._slot_map),
            len(jobs_by_slot),
        )
        self.last_worker_count = worker_count
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="provision-slot") as executor:
            futures = {
                executor.submit(self._process_slot_jobs, slot_id, slot_jobs): slot_id
                for slot_id, slot_jobs in jobs_by_slot.items()
            }
            for future in as_completed(futures):
                slot_id = futures[future]
                try:
                    results.extend(future.result())
                except Exception as exc:
                    logger.exception("Unexpected worker failure for slot %s: %s", slot_id, exc)

        return results

    def _process_slot_jobs(self, slot_id: int, jobs: list[ActivationJob]) -> list[ProvisioningResult]:
        serial = self._slot_map[slot_id]
        results: list[ProvisioningResult] = []
        for job in jobs:
            with self._coordinator.acquire(slot_id) as acquired:
                if not acquired:
                    continue
                try:
                    if self._is_human_path():
                        result = self._advance_human_job(serial, job)
                    else:
                        resolved_job = self._payload_resolver.resolve(job)
                        result = self._provisioner.provision(serial, resolved_job)
                except Exception as exc:
                    logger.exception("Provisioning failed for slot %s job=%s: %s", slot_id, job.job_id, exc)
                    result = self._retryable_or_failed(job, str(exc))

            results.append(result)
            self._report_safely(result)
            if not result.success:
                break
        return results

    def _is_human_path(self) -> bool:
        capabilities = getattr(self._provisioner, "capabilities", None)
        if not callable(capabilities):
            return False
        caps = capabilities()
        return caps is not None and getattr(caps, "unattended", True) is False

    def _ensure_record(self, job: ActivationJob) -> JobRecord:
        with self._record_lock:
            record = self._records.get(job.job_id)
            if record is None:
                record = JobRecord(job_id=job.job_id, slot_id=job.slot_id)
                self._records[job.job_id] = record
            return record

    def _advance_human_job(self, serial: str, job: ActivationJob) -> ProvisioningResult:
        record = self._ensure_record(job)
        if is_terminal(record.state) and job.job_id in self._results:
            return self._results[job.job_id]

        if record.state is JobState.RETRYABLE:
            record.advance(JobState.PREFLIGHT)
        if record.state is JobState.PENDING:
            record.advance(JobState.CLAIMED)
        if record.state is JobState.CLAIMED:
            record.advance(JobState.PREFLIGHT)

        if record.state is JobState.PREFLIGHT:
            if not self._isolation.allows(job.slot_id) or job.slot_id not in self._slot_map:
                record.advance(JobState.FAILED)
                result = ProvisioningResult.from_verdict(
                    job.job_id,
                    job.slot_id,
                    ActivationVerdict.ACTIVATION_FAILED,
                    error="preflight rejected slot",
                )
                self._results[job.job_id] = result
                return result
            record.advance(JobState.WAITING_FOR_ACTIVATION)
            submit = getattr(self._provisioner, "submit_activation", None)
            if callable(submit):
                submit(serial, job)

        if record.state is JobState.WAITING_FOR_ACTIVATION:
            snapshot = self._verification_source(job) if self._verification_source else None
            if snapshot is None:
                result = ProvisioningResult(
                    success=False,
                    job_id=job.job_id,
                    slot_id=job.slot_id,
                    error="human Settings/LPA required; activation code was not sent",
                )
                self._results[job.job_id] = result
                return result
            record.advance(JobState.ACTIVATION_SUBMITTED)
            record.advance(JobState.VERIFICATION)
            verify = getattr(self._provisioner, "verify_profile")
            result = verify(serial, job, snapshot)
            record.verdict = result.verdict
            if result.verdict is ActivationVerdict.ACTIVATION_CONFIRMED:
                record.advance(JobState.ACTIVE)
            elif result.verdict is ActivationVerdict.ACTIVATION_PARTIAL:
                record.advance(JobState.WAITING_FOR_ACTIVATION)
            elif result.verdict is ActivationVerdict.VERIFICATION_UNKNOWN:
                record.advance(JobState.RETRYABLE)
            else:
                record.advance(JobState.FAILED)
            self._results[job.job_id] = result
            return result

        return ProvisioningResult(
            success=False,
            job_id=job.job_id,
            slot_id=job.slot_id,
            error="human job is not in a runnable state",
        )

    def _fail_preflight(self, job: ActivationJob, error: str) -> ProvisioningResult:
        record = self._ensure_record(job)
        if record.state is JobState.PENDING:
            record.advance(JobState.CLAIMED)
        if record.state is JobState.CLAIMED:
            record.advance(JobState.PREFLIGHT)
        if record.state is JobState.PREFLIGHT:
            record.advance(JobState.FAILED)
        elif not is_terminal(record.state):
            record.advance(JobState.FAILED)
        result = ProvisioningResult.from_verdict(
            job.job_id,
            job.slot_id,
            ActivationVerdict.ACTIVATION_FAILED,
            error=error,
        )
        self._results[job.job_id] = result
        return result

    def _retryable_or_failed(self, job: ActivationJob, error: str) -> ProvisioningResult:
        record = self._ensure_record(job)
        if record.state is JobState.PREFLIGHT:
            record.advance(JobState.RETRYABLE)
            return ProvisioningResult(
                success=False,
                job_id=job.job_id,
                slot_id=job.slot_id,
                error=error,
                verdict=ActivationVerdict.VERIFICATION_UNKNOWN,
            )
        if not is_terminal(record.state):
            if record.state is JobState.PENDING:
                record.advance(JobState.CLAIMED)
            if record.state is JobState.CLAIMED:
                record.advance(JobState.FAILED)
            elif record.state is not JobState.FAILED:
                try:
                    record.advance(JobState.FAILED)
                except Exception:
                    pass
        return ProvisioningResult.from_verdict(
            job.job_id,
            job.slot_id,
            ActivationVerdict.ACTIVATION_FAILED,
            error=error,
        )

    def _report_safely(self, result: ProvisioningResult) -> None:
        try:
            self._source.report_result(result)
        except Exception as exc:
            with self._report_lock:
                self._pending_reports[result.job_id] = result
            logger.exception(
                "Failed to report provisioning result for slot %s job=%s: %s",
                result.slot_id,
                result.job_id,
                exc,
            )
        else:
            with self._report_lock:
                self._pending_reports.pop(result.job_id, None)

    def _flush_pending_reports(self) -> None:
        with self._report_lock:
            pending = list(self._pending_reports.values())
        for result in pending:
            self._report_safely(result)
