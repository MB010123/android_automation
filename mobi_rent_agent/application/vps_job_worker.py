"""Process pending VPS jobs (persistent; survives restart)."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from infrastructure.farm_task_client import FarmTaskClient
from infrastructure.slot_assignment_store import SlotAssignmentStore
from infrastructure.slot_event_store import SlotEventStore
from infrastructure.vps_job_store import VpsJobStore

logger = logging.getLogger("vps_backend.job_worker")


class VpsJobWorker:
    def __init__(
        self,
        *,
        job_store: VpsJobStore,
        assignment_store: SlotAssignmentStore,
        event_store: SlotEventStore,
        farm_task_client: FarmTaskClient | None,
        poll_interval_seconds: float = 2.0,
    ) -> None:
        self._jobs = job_store
        self._assignments = assignment_store
        self._events = event_store
        self._farm = farm_task_client
        self._poll_interval = poll_interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="vps-job-worker")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def enqueue_process(self, job_id: str) -> None:
        threading.Thread(
            target=self.process_job,
            args=(job_id,),
            daemon=True,
            name=f"vps-job-{job_id[:8]}",
        ).start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            for record in self._jobs.list_pending(limit=20):
                self.process_job(record.job_id)
            self._stop.wait(self._poll_interval)

    def process_job(self, job_id: str) -> None:
        record = self._jobs.get(job_id)
        if record is None or record.status != "pending":
            return
        if self._farm is None:
            self._jobs.update(job_id, status="failed", error="farm_unreachable", progress=0)
            return
        now = time.time()
        self._jobs.update(job_id, status="running", progress=10, started_at=now)
        if record.farm_slot_id is not None:
            self._events.append(
                record.farm_slot_id,
                f"{record.type}_started",
                f"job_id={job_id}",
            )
        response = self._farm.run_task(
            task_type=record.type,
            farm_slot_id=int(record.farm_slot_id or 0),
            payload=record.request_payload,
            job_id=job_id,
        )
        if response.ok:
            self._jobs.update(
                job_id,
                status="done",
                progress=100,
                result_payload=response.body,
                error=None,
                completed_at=time.time(),
            )
            if record.type == "assign" and record.farm_slot_id is not None:
                self._events.append(
                    record.farm_slot_id,
                    "assignment_completed",
                    f"job_id={job_id}",
                )
            elif record.farm_slot_id is not None:
                self._events.append(
                    record.farm_slot_id,
                    f"{record.type}_completed",
                    f"job_id={job_id}",
                )
            logger.info("vps_job_done job_id=%s type=%s", job_id, record.type)
            return
        error = response.error or "task_failed"
        self._jobs.update(
            job_id,
            status="failed",
            progress=0,
            error=error,
            completed_at=time.time(),
        )
        if record.type == "assign" and record.farm_slot_id is not None:
            self._assignments.release(record.farm_slot_id)
            self._events.append(
                record.farm_slot_id,
                "assignment_failed",
                error,
            )
        elif record.farm_slot_id is not None:
            self._events.append(record.farm_slot_id, f"{record.type}_failed", error)
        logger.warning("vps_job_failed job_id=%s error=%s", job_id, error)
