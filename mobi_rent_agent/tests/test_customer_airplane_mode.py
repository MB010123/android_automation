"""Customer airplane_mode action goes through Farm cmd connectivity."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.fakes_supabase import MemoryTenant
from tests.test_esim_setup_mode import _assert_no_internals, _slot2_service
from tests.test_remote_access_poc import CUSTOMER_A, CUSTOMER_B, FakeFarm, _rental


def test_authorized_airplane_mode_uses_farm_task(tmp_path: Path):
    farm = FakeFarm()
    _tenant, platform, farm, service, _store, rental = _slot2_service(tmp_path, farm=farm)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    for state in ("on", "off"):
        result = service.control_session(
            CUSTOMER_A, rental, {"action": "airplane_mode", "state": state}
        )
        assert result.http_status == 200, state
        assert result.body["ok"] is True
        _assert_no_internals(result.body)
        assert "udid" not in json.dumps(result.body).lower()
    modes = [t for t in farm.tasks if t["type"] == "airplane_mode"]
    assert [t["payload"]["state"] for t in modes] == ["on", "off"]
    assert not any(c[0] in {"revoke", "release"} for c in platform.calls)
    source = (ROOT / "application" / "remote_access_service.py").read_text(encoding="utf-8")
    assert "settings put" not in source
    assert "cmd connectivity" not in source
    assert "WRITE_SECURE_SETTINGS" not in source


def test_airplane_mode_rejects_other_rental_and_device_targeting(tmp_path: Path):
    farm = FakeFarm()
    tenant, _platform, farm, service, _store, rental = _slot2_service(tmp_path, farm=farm)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    stolen = service.control_session(
        CUSTOMER_B, rental, {"action": "airplane_mode", "state": "on"}
    )
    assert stolen.http_status == 403
    assert stolen.body["error"] == "rental_not_owned"
    for key, value in (
        ("slot_id", 2),
        ("udid", "OTHER"),
        ("serial", "OTHER"),
        ("workspace_id", "ws-other"),
    ):
        denied = service.control_session(
            CUSTOMER_A, rental, {"action": "airplane_mode", "state": "off", key: value}
        )
        assert denied.http_status == 403, key
        assert denied.body["error"] == "forbidden_control"
    other = _rental(tenant, bay=3, user_id=CUSTOMER_A)
    unassigned = service.control_session(
        CUSTOMER_A, other, {"action": "airplane_mode", "state": "on"}
    )
    assert unassigned.http_status in {403, 404, 409, 503}
    assert unassigned.body.get("ok") is False
    done = service.complete_setup(CUSTOMER_A, rental)
    assert done.http_status == 200
    inactive = service.control_session(
        CUSTOMER_A, rental, {"action": "airplane_mode", "state": "on"}
    )
    assert inactive.http_status in {403, 404, 409}
    assert inactive.body.get("ok") is False


def test_airplane_mode_missing_state_is_invalid(tmp_path: Path):
    farm = FakeFarm()
    _tenant, _platform, farm, service, _store, rental = _slot2_service(tmp_path, farm=farm)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    missing = service.control_session(CUSTOMER_A, rental, {"action": "airplane_mode"})
    assert missing.http_status == 422
    assert missing.body["error"] == "invalid_control"
    assert not any(t["type"] == "airplane_mode" for t in farm.tasks)
