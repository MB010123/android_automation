"""CLEANUP REQUIRED blocks inventory until admin verification."""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_rental_end import OWNER_A, OWNER_B, _available, _harness, _seed
from tests.fakes_supabase import seed_owned_slot

FAKE_QR = "https://example.test/slots/rental/qr.png"


def test_cleanup_required_blocks_reassignment_until_admin_verify(tmp_path: Path):
    svc, _w, _j, _a, reservations, tenant, _e, _g = _harness(tmp_path)
    rental = _seed(tenant, bay=1, rental_id=str(uuid.uuid4()), user_id=OWNER_A)
    assert svc.reserve_slot(1, {"rental_id": rental}).http_status == 200
    ended = svc.end_rental(rental, explicit=True)
    assert ended.http_status == 200
    assert ended.body["device_cleanup"] == "required"
    assert 1 not in _available(svc)

    second = str(uuid.uuid4())
    tenant.ensure_profile(OWNER_B, "b@example.com")
    seed_owned_slot(tenant, bay=1, rental_id=second, user_id=OWNER_B, qr_code_url=FAKE_QR)
    blocked = svc.reserve_slot(1, {"rental_id": second, "user_id": OWNER_B})
    assert blocked.http_status == 409
    assert blocked.body["error"] == "cleanup_required"
    assigned = svc.assign_slot(
        1,
        {"rental_id": second, "esim_qr_url": FAKE_QR, "user_id": OWNER_B},
    )
    assert assigned.http_status == 409
    assert assigned.body["error"] == "cleanup_required"
    verified = svc.verify_cleanup(1)
    assert verified.http_status == 200
    assert 1 in _available(svc)
    claimed = svc.reserve_slot(1, {"rental_id": second, "user_id": OWNER_B})
    assert claimed.http_status == 200
    assert reservations.get(1).rental_id == second


def test_customer_cancel_revokes_and_holds_cleanup(tmp_path: Path):
    svc, _w, _j, _a, reservations, tenant, _e, gads = _harness(tmp_path)
    rental = _seed(tenant, bay=1, rental_id=str(uuid.uuid4()), user_id=OWNER_A)
    assert svc.reserve_slot(1, {"rental_id": rental}).http_status == 200
    cancelled = svc.cancel_rental_for_customer(OWNER_A, rental)
    assert cancelled.http_status == 200
    assert gads.calls == [(1, rental)]
    assert reservations.is_reserved(1) is False
    assert tenant.owner_of_slot(1) is None
    assert 1 not in _available(svc)
    stranger = svc.cancel_rental_for_customer(OWNER_B, rental)
    assert stranger.http_status == 403


def test_verify_rejected_while_still_occupied(tmp_path: Path):
    svc, _w, _j, _a, reservations, tenant, _e, _g = _harness(tmp_path)
    rental = _seed(tenant, bay=1, rental_id=str(uuid.uuid4()), user_id=OWNER_A)
    assert svc.reserve_slot(1, {"rental_id": rental}).http_status == 200
    blocked = svc.verify_cleanup(1)
    assert blocked.http_status == 409
    assert reservations.is_reserved(1) is True
