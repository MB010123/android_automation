"""VPS API contract helpers."""
from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.vps_api_contract import (
    assign_acceptance_body,
    failure_class,
    job_response_body,
    provisioning_phase,
)
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


def test_provisioning_phase_android_authority_refusal_is_manual_action():
    """Farm assign job 9dced20d stored this exact refusal. It is not a dead bay."""
    now = time.time()
    record = VpsJobRecord(
        job_id="9dced20d-60a7-4809-a911-eecf5c2564a5",
        type="assign",
        farm_slot_id=1,
        status="failed",
        progress=0,
        request_payload={},
        result_payload={
            "message": (
                "no legitimate Android eSIM authority (no legitimate Android eSIM "
                "authority: WRITE_EMBEDDED_SUBSCRIPTIONS, carrier privileges, "
                "Device Owner, Profile Owner, MANAGE_DEVICE_POLICY_MANAGED_SUBSCRIPTIONS. "
                "REAL_ESIM_ENABLED=true is not authorization.)"
            )
        },
        error="provisioning_failed",
        idempotency_key="assign-x",
        created_at=now,
        updated_at=now,
        started_at=now,
        completed_at=now,
    )
    assert failure_class(record) == "requires_manual_action"
    assert provisioning_phase(record) == "requires_manual_action"
