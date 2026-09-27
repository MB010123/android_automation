"""Process pending VPS jobs (persistent; survives restart)."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from application.install_state import (
    INSTALL_ACCEPTED,
    INSTALL_FAILED,
    INSTALL_STATES,
    INSTALL_VERIFICATION_UNKNOWN,
    INSTALL_VERIFIED,
)
from infrastructure.farm_task_client import FarmTaskResponse
from infrastructure.slot_assignment_store import SlotAssignmentStore
from infrastructure.slot_event_store import SlotEventStore
from infrastructure.vps_job_store import VpsJobRecord, VpsJobStore

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
        auth_store: Any = None,
        clock: Callable[[], float] | None = None,
        running_stale_seconds: float = 180.0,
    ) -> None:
        self._jobs = job_store
        self._assignments = assignment_store
        self._events = event_store
        self._farm = farm_task_client
        self._poll_interval = poll_interval_seconds
        self._auth_store = auth_store
        self._clock = clock or time.time
        self._running_stale_seconds = running_stale_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="vps-job-worker")
        self._thread.start()

    def stop(self, join_timeout: float | None = None) -> None:
        """Signal the loop to exit; optionally wait (bounded) for it to finish."""
        self._stop.set()
        thread = self._thread
        if join_timeout is not None and thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, join_timeout))

    def enqueue_process(self, job_id: str) -> None:
        threading.Thread(
            target=self.process_job,
            args=(job_id,),
            daemon=True,
            name=f"vps-job-{job_id[:8]}",
        ).start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            for record in self._jobs.list_running(limit=20):
                self.recover_running_job(record.job_id)
            for record in self._jobs.list_pending(limit=20):
                self.process_job(record.job_id)
            self._stop.wait(self._poll_interval)

    def process_job(self, job_id: str) -> None:
        record = self._jobs.get(job_id)
        if record is None:
            return
        if record.status == "running":
            self.recover_running_job(job_id)
            return
        if record.status != "pending":
            return
        if self._farm is None:
            self._jobs.update(job_id, status="failed", error="farm_unreachable", progress=0)
            return
        now = self._clock()
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
        self._apply_farm_response(record, response)

    def recover_running_job(self, job_id: str) -> None:
        """Recover a stale running assign without blindly resending provision_esim.

        Query the Farm Agent job cache first. Retry run_task only when that
        cache proves the previous attempt failed before the activation code
        was sent. A cache miss is INSTALL_VERIFICATION_UNKNOWN.
        """
        record = self._jobs.get(job_id)
        if record is None or record.status != "running":
            return
        started = record.started_at or record.updated_at
        if self._clock() - started < self._running_stale_seconds:
            return
        if record.type != "assign":
            self._finish_unknown(record, recovered=True)
            return
        cached = self._lookup_farm_job(job_id)
        if cached is None:
            logger.warning("running_job_recovered_without_redownload job_id=%s", job_id)
            self._finish_unknown(record, recovered=True)
            return
        body = cached.body if isinstance(cached.body, dict) else {}
        install_state = str(body.get("install_state") or "")
        sent = bool(body.get("activation_code_sent"))
        if install_state == INSTALL_FAILED and not sent:
            if self._farm is None:
                self._apply_farm_response(record, cached)
                return
            logger.info("running_job_retry_pre_send_failure job_id=%s", job_id)
            response = self._farm.run_task(
                task_type=record.type,
                farm_slot_id=int(record.farm_slot_id or 0),
                payload=record.request_payload,
                job_id=job_id,
            )
            self._apply_farm_response(record, response)
            return
        logger.info("running_job_recovered_from_farm_cache job_id=%s", job_id)
        self._apply_farm_response(record, cached)

    def _lookup_farm_job(self, job_id: str) -> FarmTaskResponse | None:
        if self._farm is None:
            return None
        lookup = getattr(self._farm, "lookup_job", None)
        if not callable(lookup):
            return None
        try:
            found = lookup(job_id)
        except (TypeError, ValueError, OSError):
            return None
        if found is None:
            return None
        if not isinstance(found, FarmTaskResponse):
            return None
        if found.http_status == 404 or not found.body:
            return None
        return found

    def _finish_unknown(self, record: VpsJobRecord, *, recovered: bool) -> None:
        job_id = record.job_id
        result = {
            "install_state": INSTALL_VERIFICATION_UNKNOWN,
            "activation_code_sent": True,
            "recovered": recovered,
        }
        self._jobs.update(
            job_id,
            status="done",
            progress=100,
            result_payload=result,
            error=None,
            completed_at=self._clock(),
        )
        if record.farm_slot_id is not None:
            self._events.append(
                record.farm_slot_id,
                "provisioning_verification_unknown",
                f"job_id={job_id} recovered_running",
            )

    def _apply_farm_response(self, record: VpsJobRecord, response: FarmTaskResponse) -> None:
        job_id = record.job_id
        body = response.body if isinstance(response.body, dict) else {}
        install_state = str(body.get("install_state") or "")
        if install_state not in INSTALL_STATES:
            if response.ok:
                install_state = INSTALL_VERIFIED if record.type != "assign" else INSTALL_ACCEPTED
            else:
                install_state = INSTALL_FAILED
        sent = bool(body.get("activation_code_sent"))
        if record.type != "assign":
            self._apply_action_response(record, response)
            return

        result_payload = {
            "install_state": install_state,
            "activation_code_sent": sent,
        }
        if body.get("message"):
            result_payload["message"] = body.get("message")
        if body.get("error"):
            result_payload["error"] = body.get("error")

        if install_state == INSTALL_FAILED and not sent:
            self._jobs.update(
                job_id,
                status="failed",
                progress=0,
                error=response.error or "task_failed",
                result_payload=result_payload,
                completed_at=self._clock(),
            )
            if record.farm_slot_id is not None:
                self._assignments.release(record.farm_slot_id)
                self._events.append(record.farm_slot_id, "provisioning_failed", response.error or "task_failed")
                self._events.append(record.farm_slot_id, "assignment_failed", response.error or "task_failed")
            logger.warning("farm_task_failed job_id=%s type=%s error=%s", job_id, record.type, response.error)
            return

        if install_state == INSTALL_FAILED and sent:
            result_payload["install_state"] = INSTALL_VERIFICATION_UNKNOWN
            self._jobs.update(
                job_id,
                status="done",
                progress=100,
                result_payload=result_payload,
                error=None,
                completed_at=self._clock(),
            )
            if record.farm_slot_id is not None:
                self._events.append(
                    record.farm_slot_id,
                    "provisioning_verification_unknown",
                    f"job_id={job_id}",
                )
            return

        status = "done"
        if install_state == INSTALL_VERIFIED:
            event = "provisioning_completed"
        elif install_state == INSTALL_ACCEPTED:
            event = "provisioning_accepted"
        else:
            event = "provisioning_verification_unknown"
        recorded = self._record_esim_if_needed(record, install_state)
        if recorded is False:
            result_payload["tenant_record_error"] = True
        self._jobs.update(
            job_id,
            status=status,
            progress=100,
            result_payload=result_payload,
            error=None,
            completed_at=self._clock(),
        )
        if record.farm_slot_id is not None:
            self._events.append(record.farm_slot_id, event, f"job_id={job_id}")
            if install_state == INSTALL_VERIFIED:
                self._events.append(record.farm_slot_id, "assignment_completed", f"job_id={job_id}")
        logger.info("farm_task_completed job_id=%s type=%s bay=%s", job_id, record.type, record.farm_slot_id)

    def _record_esim_if_needed(self, record: VpsJobRecord, install_state: str) -> bool | None:
        if install_state not in {INSTALL_ACCEPTED, INSTALL_VERIFIED}:
            return None
        if self._auth_store is None or not callable(getattr(self._auth_store, "record_esim_upload", None)):
            return None
        payload = record.request_payload if isinstance(record.request_payload, dict) else {}
        user_id = str(payload.get("user_id") or "").strip()
        storage_key = str(payload.get("esim_storage_key") or payload.get("esim_qr_url") or "").strip()
        if not user_id or not storage_key or record.farm_slot_id is None:
            return None
        try:
            uploaded = self._auth_store.record_esim_upload(
                user_id=user_id,
                farm_slot_id=int(record.farm_slot_id),
                storage_key=storage_key,
                rental_id=str(payload.get("rental_id") or "") or None,
                carrier=str(payload.get("carrier") or payload.get("carrier_name") or "") or None,
                job_id=record.job_id,
            )
        except (TypeError, ValueError, OSError):
            logger.warning("lovable_esim_record_failed job_id=%s", record.job_id)
            return False
        if uploaded == "":
            return False
        return True

    def _apply_action_response(self, record: VpsJobRecord, response: FarmTaskResponse) -> None:
        job_id = record.job_id
        if response.ok:
            self._jobs.update(
                job_id,
                status="done",
                progress=100,
                result_payload=response.body if isinstance(response.body, dict) else {},
                error=None,
                completed_at=self._clock(),
            )
            if record.farm_slot_id is not None:
                self._events.append(
                    record.farm_slot_id,
                    f"{record.type}_completed",
                    f"job_id={job_id}",
                )
            return
        error = response.error or "task_failed"
        self._jobs.update(
            job_id,
            status="failed",
            progress=0,
            error=error,
            result_payload=response.body if isinstance(response.body, dict) else {},
            completed_at=self._clock(),
        )
        if record.farm_slot_id is not None:
            self._events.append(record.farm_slot_id, f"{record.type}_failed", error)
