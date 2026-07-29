"""Application use case for isolated, zero-touch eSIM provisioning."""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from domain.models import ActivationJob, ProvisioningResult
from domain.ports import ActivationJobSource, ActivationPayloadResolver, SubscriptionProvisioner
from application.slot_coordinator import SlotOperationCoordinator

logger = logging.getLogger("mobi_rent_agent.provisioning")


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
    """Polls activation work and provisions different slots independently."""

    def __init__(
        self,
        config: ProvisioningServiceConfig,
        source: ActivationJobSource,
        provisioner: SubscriptionProvisioner,
        payload_resolver: ActivationPayloadResolver,
        slot_map: dict[int, str],
        coordinator: SlotOperationCoordinator | None = None,
    ) -> None:
        self._config = config
        self._source = source
        self._provisioner = provisioner
        self._payload_resolver = payload_resolver
        self._slot_map = dict(slot_map)
        self._coordinator = coordinator or SlotOperationCoordinator(list(slot_map))
        self._stop_event = threading.Event()
        self._cycle_lock = threading.Lock()
        self._pending_reports: dict[str, ProvisioningResult] = {}
        self._report_lock = threading.Lock()

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
            jobs = list(self._source.fetch_pending(self._slot_map.keys()))
        except Exception as exc:
            logger.exception("Failed to fetch activation jobs: %s", exc)
            return []

        jobs_by_slot: dict[int, list[ActivationJob]] = defaultdict(list)
        results: list[ProvisioningResult] = []
        for job in jobs:
            if job.slot_id not in self._slot_map:
                result = ProvisioningResult(
                    success=False,
                    job_id=job.job_id,
                    slot_id=job.slot_id,
                    error="slot is not managed by this agent",
                )
                results.append(result)
                self._report_safely(result)
                continue
            jobs_by_slot[job.slot_id].append(job)

        if not jobs_by_slot:
            return results

        worker_count = min(self._config.max_workers, len(jobs_by_slot))
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
                    resolved_job = self._payload_resolver.resolve(job)
                    result = self._provisioner.provision(serial, resolved_job)
                except Exception as exc:
                    logger.exception("Provisioning failed for slot %s job=%s: %s", slot_id, job.job_id, exc)
                    result = ProvisioningResult(
                        success=False,
                        job_id=job.job_id,
                        slot_id=slot_id,
                        error=str(exc),
                    )

            results.append(result)
            self._report_safely(result)
            if not result.success:
                break
        return results

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
