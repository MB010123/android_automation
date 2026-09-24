"""VPS API contract helpers."""
from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.vps_api_contract import assign_acceptance_body, job_response_body, provisioning_phase
from infrastructure.vps_job_store import VpsJobRecord


def test_assign_acceptance_shape():
    body = assign_acceptance_body(job_id=str(uuid.uuid4()), bay=3)
    assert body["ok"] is True
    assert body["bay"] == 3
    assert body["status"] == "pending"
    assert "slot_id" in body


def test_provisioning_phase_manual_action():
    now = time.time()
    record = VpsJobRecord(
        job_id="j1",
        type="assign",
        farm_slot_id=1,
        status="failed",
        progress=0,
        request_payload={},
        result_payload={"message": "human Settings/LPA required"},
        error="provisioning_failed",
        idempotency_key="assign-x",
        created_at=now,
        updated_at=now,
        started_at=now,
        completed_at=now,
    )
    assert provisioning_phase(record) == "requires_manual_action"
    body = job_response_body(record)
    assert body["provisioning_phase"] == "requires_manual_action"
    assert body["created_at"] is not None
