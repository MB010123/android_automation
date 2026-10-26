"""Spinner-gated eSIM setup: QR walk-once + setup_ui_state."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.esim_setup_ui_state import (
    SETUP_UI_AWAITING,
    SETUP_UI_COMPLETED,
    SETUP_UI_WORKING,
    classify_setup_ui,
)
from application.setup_activity_guard import should_recover_setup
from tests.fakes_supabase import MemoryTenant
from tests.test_esim_auto_activation import CONFIRM_UI, SEARCHING_UI, SIM_MANAGER_UI
from tests.test_esim_qr_upload import PNG_BYTES, _mapped
from tests.test_remote_access_poc import (
    CUSTOMER_A,
    FakeFarm,
    FakePlatform,
    _rental,
    _service,
)


def test_classifier_progress_is_working():
    hit = classify_setup_ui(SEARCHING_UI)
    assert hit.state == SETUP_UI_WORKING
    assert hit.reason == "progress_copy"


def test_classifier_confirm_dialog_is_awaiting():
    hit = classify_setup_ui(CONFIRM_UI)
    assert hit.state == SETUP_UI_AWAITING
    assert hit.human_click_label == "Add"
    assert "Add" in (hit.prompt or "")


def test_classifier_walk_in_progress_hides_gallery():
    hit = classify_setup_ui(
        'text="Select item" text="Done"',
        walk_in_progress=True,
    )
    assert hit.state == SETUP_UI_WORKING
    assert hit.reason == "walk_in_progress"


def test_classifier_sim_manager_after_walk_is_completed():
    hit = classify_setup_ui(SIM_MANAGER_UI, walk_completed=True)
    assert hit.state == SETUP_UI_COMPLETED
    assert hit.reason == "back_on_sim_manager"


def test_classifier_installed_profile_is_completed():
    hit = classify_setup_ui(SIM_MANAGER_UI, esim_profile_present=True)
    assert hit.state == SETUP_UI_COMPLETED


def test_guard_does_not_recover_after_walk_or_profile():
    assert should_recover_setup(allowed=False, qr_walk_completed=True) is False
    assert should_recover_setup(allowed=False, esim_profile_present=True) is False
    assert should_recover_setup(allowed=False) is True
    assert should_recover_setup(allowed=True) is False


def test_samsung_status_and_control_expose_setup_ui_state(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    serial = "RFCNB056WEV"
    service, store, _ = _service(
        tmp_path,
        tenant=tenant,
        platform=FakePlatform(registered={serial}),
        farm=farm,
        allowed=(21,),
        slot_map={21: serial},
        farm_status=_mapped(21),
        gads_slot_ids=(21,),
        workspace_map={21: "ws-21"},
        prepare_slot_ids=(21,),
        observe_slot_ids=(21,),
    )
    rental = _rental(tenant, bay=21, user_id=CUSTOMER_A)
    created = service.create_remote_access(CUSTOMER_A, None, rental)
    assert created.http_status == 201, created.body
    uploaded = service.upload_esim_qr(CUSTOMER_A, rental, PNG_BYTES)
    assert uploaded.http_status == 200
    assert uploaded.body["setup_ui_state"] == SETUP_UI_WORKING
    session = store.get(rental)
    assert session is not None
    assert session.qr_sha256 == hashlib.sha256(PNG_BYTES).hexdigest()
    assert session.qr_walk_completed is True
    status = service.device_status_for_customer(CUSTOMER_A, rental)
    assert status.http_status == 200
    assert status.body["setup_ui_state"] in {
        SETUP_UI_WORKING,
        SETUP_UI_AWAITING,
        SETUP_UI_COMPLETED,
    }
    activation = service.activation_status_for_customer(CUSTOMER_A, rental)
    assert activation.http_status == 200
    assert "setup_ui_state" in activation.body
    store.set_setup_ui_state(rental, SETUP_UI_AWAITING)
    tapped = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 10, "y": 20})
    assert tapped.http_status == 200
    assert tapped.body["setup_ui_state"] == SETUP_UI_WORKING
    farm.tasks.clear()
    service._launch_add_esim(21)
    auto = [t for t in farm.tasks if t["type"] == "esim_auto_activation"]
    assert auto == []
    inspects = [t for t in farm.tasks if t["type"] == "setup_session_inspect"]
    assert inspects
    assert inspects[-1]["payload"].get("qr_walk_completed") is True
    assert inspects[-1]["payload"].get("recover") is True
