"""Authoritative Supabase slots.imei2 and carrier_name on user slot GETs."""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infrastructure.slot_public_id import public_id_for_farm_slot
from tests.fakes_supabase import MemoryTenant
from tests.test_blocker_fixes import _farm_svc
from tests.test_vps_user_auth import FARM_TOKEN, STRONG, _http, _start_auth_server

SLOT1 = public_id_for_farm_slot(1)
SLOT2 = public_id_for_farm_slot(2)
IMEI2 = "353456789012345"


def test_owned_slot_returns_supabase_imei2_and_carrier_name(tmp_path: Path):
    tenant = MemoryTenant()
    tenant.claim_slot(1, "user-a", None)
    tenant.slots[1]["imei2"] = f"  {IMEI2}  "
    tenant.slots[1]["carrier_name"] = "Verizon"
    svc, _, _ = _farm_svc(tmp_path, tenant)

    listed = svc.list_slots_for_user("user-a")
    assert listed.http_status == 200
    assert listed.body["count"] == 1
    item = listed.body["slots"][0]
    assert item["imei2"] == IMEI2
    assert item["carrier_name"] == "Verizon"
    assert item["imei2_status"] == "known"

    detail = svc.get_slot_record(SLOT1)
    assert detail.http_status == 200
    assert detail.body["imei2"] == IMEI2
    assert detail.body["carrier_name"] == "Verizon"
    assert detail.body["imei2_status"] == "known"


def test_missing_imei2_is_null_and_carrier_name_still_returned(tmp_path: Path):
    tenant = MemoryTenant()
    tenant.claim_slot(1, "user-a", None)
    tenant.slots[1]["imei2"] = "   "
    tenant.slots[1]["carrier_name"] = "T-Mobile"
    svc, _, _ = _farm_svc(tmp_path, tenant)

    listed = svc.list_slots_for_user("user-a")
    item = listed.body["slots"][0]
    assert item["imei2"] is None
    assert item["carrier_name"] == "T-Mobile"
    assert item["imei2_status"] == "unknown"

    detail = svc.get_slot_record(SLOT1)
    assert detail.body["imei2"] is None
    assert detail.body["carrier_name"] == "T-Mobile"
    assert detail.body["imei2_status"] == "unknown"


def test_other_user_list_does_not_include_imei2(tmp_path: Path):
    tenant = MemoryTenant()
    tenant.claim_slot(1, "user-a", None)
    tenant.slots[1]["imei2"] = IMEI2
    tenant.slots[1]["carrier_name"] = "Verizon"
    svc, _, _ = _farm_svc(tmp_path, tenant)

    other = svc.list_slots_for_user("user-b")
    assert other.body["slots"] == []
    assert other.body["count"] == 0
    assert IMEI2 not in str(other.body)


def test_http_user_slot_imei2_isolation_and_payload_ignored(tmp_path: Path):
    server, port, tenant, _auth = _start_auth_server(tmp_path)
    base = f"http://127.0.0.1:{port}"
    try:
        _, a, _ = _http("POST", f"{base}/auth/signup", body={"email": "imei-a@example.com", "password": STRONG})
        _, b, _ = _http("POST", f"{base}/auth/signup", body={"email": "imei-b@example.com", "password": STRONG})
        token_a = a["session"]["access_token"]
        token_b = b["session"]["access_token"]
        user_a = a["user"]["id"]
        rental = str(uuid.uuid4())

        status, farm_job, _ = _http(
            "POST",
            f"{base}/farm/slots/1/assign",
            token=FARM_TOKEN,
            body={
                "rental_id": rental,
                "esim_qr_url": "https://example.test/private/qr",
                "carrier": "Verizon",
                "user_id": user_a,
                "imei2": "000000000000000",
            },
        )
        assert status == 202
        assert farm_job["ok"] is True
        tenant.slots[1]["imei2"] = IMEI2
        tenant.slots[1]["carrier_name"] = "Verizon"

        status, listed, _ = _http("GET", f"{base}/slots", token=token_a)
        assert status == 200
        assert listed["count"] == 1
        assert listed["slots"][0]["imei2"] == IMEI2
        assert listed["slots"][0]["carrier_name"] == "Verizon"

        status, detail, _ = _http("GET", f"{base}/slots/{SLOT1}", token=token_a)
        assert status == 200
        assert detail["imei2"] == IMEI2
        assert detail["carrier_name"] == "Verizon"
        assert "000000000000000" not in str(detail)

        status, hidden, _ = _http("GET", f"{base}/slots/{SLOT1}", token=token_b)
        assert status == 404
        assert hidden.get("error") == "slot_not_found"
        leaked = json.dumps(hidden)
        assert IMEI2 not in leaked
        assert hidden.get("imei2") != IMEI2

        status, other_list, _ = _http("GET", f"{base}/slots", token=token_b)
        assert status == 200
        assert other_list["slots"] == []
        assert IMEI2 not in json.dumps(other_list)

        tenant.claim_slot(2, user_a, None)
        tenant.slots[2]["carrier_name"] = "AT&T"
        tenant.slots[2].pop("imei2", None)
        status, missing, _ = _http("GET", f"{base}/slots/{SLOT2}", token=token_a)
        assert status == 200
        assert missing["imei2"] is None
        assert missing["carrier_name"] == "AT&T"
        assert missing["imei2_status"] == "unknown"

        status, user_esim, _ = _http(
            "POST",
            f"{base}/slots/{SLOT1}/esim",
            token=token_a,
            body={
                "rental_id": str(uuid.uuid4()),
                "qr_code_url": "users/a/esim/qr",
                "carrier": "Verizon",
                "imei2": "111111111111111",
            },
        )
        assert status in {202, 409}
        status, after, _ = _http("GET", f"{base}/slots/{SLOT1}", token=token_a)
        assert status == 200
        assert after["imei2"] == IMEI2
        assert after["carrier_name"] == "Verizon"
    finally:
        server.shutdown()
