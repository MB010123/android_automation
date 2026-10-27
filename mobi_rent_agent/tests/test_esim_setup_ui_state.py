"""Spinner-gated eSIM setup: QR walk-once + setup_ui_state."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dataclasses import replace

from application.esim_setup_ui_state import (
    SETUP_TIMEOUT_BANNER,
    SETUP_UI_AWAITING,
    SETUP_UI_COMPLETED,
    SETUP_UI_WORKING,
    SETUP_WORKING_FALLBACK_SECONDS,
    SIM_MANAGER_HANDOFF_PROMPT,
    classify_setup_ui,
    decide_setup_surface,
)
from application.setup_activity_guard import should_recover_setup
from tests.fakes_supabase import MemoryTenant
from tests.test_esim_auto_activation import CONFIRM_UI, SEARCHING_UI, SIM_MANAGER_UI
from tests.test_esim_qr_upload import PNG_BYTES, _mapped
from tests.test_remote_access_poc import (
    CUSTOMER_A,
    FakeClock,
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


def test_classifier_sim_manager_is_handoff():
    hit = classify_setup_ui(SIM_MANAGER_UI, walk_completed=True)
    assert hit.state == SETUP_UI_AWAITING
    assert hit.reason == "sim_manager_handoff"
    assert hit.prompt == SIM_MANAGER_HANDOFF_PROMPT
    before_walk = classify_setup_ui(SIM_MANAGER_UI)
    assert before_walk.state == SETUP_UI_AWAITING
    assert before_walk.reason == "sim_manager_handoff"


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
    assert uploaded.body["setup_ui_state"] == SETUP_UI_AWAITING
    assert "Add eSIM" in str(uploaded.body.get("setup_ui_prompt") or "")
    session = store.get(rental)
    assert session is not None
    assert session.qr_sha256 == hashlib.sha256(PNG_BYTES).hexdigest()
    assert session.qr_walk_completed is True
    status = service.device_status_for_customer(CUSTOMER_A, rental)
    assert status.http_status == 200
    assert status.body["setup_ui_state"] == SETUP_UI_AWAITING
    activation = service.activation_status_for_customer(CUSTOMER_A, rental)
    assert activation.http_status == 200
    assert activation.body["setup_ui_state"] == SETUP_UI_AWAITING
    tapped = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 10, "y": 20})
    assert tapped.http_status == 200
    assert tapped.body["setup_ui_state"] == SETUP_UI_AWAITING
    farm.tasks.clear()
    service._launch_add_esim(21)
    auto = [t for t in farm.tasks if t["type"] == "esim_auto_activation"]
    assert auto == []
    inspects = [t for t in farm.tasks if t["type"] == "setup_session_inspect"]
    assert inspects
    assert inspects[-1]["payload"].get("qr_walk_completed") is True
    assert inspects[-1]["payload"].get("recover") is True


def test_working_spinner_stays_locked_before_45_seconds():
    surface = decide_setup_surface(
        setup_ui_state=SETUP_UI_WORKING,
        elapsed_working_seconds=44,
        setup_mode=True,
    )
    assert surface.show_spinner is True
    assert surface.show_phone is False
    assert surface.controls_enabled is False
    assert surface.timed_out is False
    assert surface.banner is None


def test_working_spinner_unlocks_after_45_seconds():
    surface = decide_setup_surface(
        setup_ui_state=SETUP_UI_WORKING,
        elapsed_working_seconds=SETUP_WORKING_FALLBACK_SECONDS,
        setup_mode=True,
    )
    assert surface.show_spinner is False
    assert surface.show_phone is True
    assert surface.controls_enabled is True
    assert surface.timed_out is True
    assert surface.show_retry is True
    assert surface.banner == SETUP_TIMEOUT_BANNER


def test_server_fallback_or_farm_timeout_unlocks_working_spinner():
    by_server = decide_setup_surface(
        setup_ui_state=SETUP_UI_WORKING,
        elapsed_working_seconds=1,
        setup_mode=True,
        server_manual_fallback=True,
    )
    by_farm = decide_setup_surface(
        setup_ui_state=SETUP_UI_WORKING,
        elapsed_working_seconds=1,
        setup_mode=True,
        automated_setup_timed_out=True,
    )
    assert by_server.timed_out is True and by_farm.timed_out is True
    assert by_server.controls_enabled is True
    awaiting = decide_setup_surface(
        setup_ui_state=SETUP_UI_AWAITING,
        elapsed_working_seconds=90,
        setup_mode=True,
    )
    assert awaiting.show_spinner is False
    assert awaiting.timed_out is False
    assert awaiting.controls_enabled is True


def test_device_status_unlocks_manual_fallback_after_45_seconds(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    serial = "RFCNB056WEV"
    clock = FakeClock()
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
        clock=clock,
    )
    rental = _rental(tenant, bay=21, user_id=CUSTOMER_A)
    created = service.create_remote_access(CUSTOMER_A, None, rental)
    assert created.http_status == 201, created.body
    uploaded = service.upload_esim_qr(CUSTOMER_A, rental, PNG_BYTES)
    assert uploaded.http_status == 200
    session = store.get(rental)
    assert session is not None
    store.upsert(
        replace(
            session,
            qr_walk_completed=False,
            setup_complete=False,
            setup_ui_state=SETUP_UI_WORKING,
        )
    )
    service._qr_walk_started[rental] = session.qr_sha256 or "pending"
    service._setup_working_since[rental] = clock.now
    service._setup_manual_unlocked.discard(rental)
    early = service.device_status_for_customer(CUSTOMER_A, rental)
    assert early.http_status == 200
    assert early.body.get("setup_ui_state") == SETUP_UI_WORKING
    assert early.body.get("setup_manual_fallback") is not True
    clock.now += SETUP_WORKING_FALLBACK_SECONDS
    farm.tasks.clear()
    status = service.device_status_for_customer(CUSTOMER_A, rental)
    assert status.http_status == 200
    assert status.body["setup_ui_state"] == SETUP_UI_WORKING
    assert status.body.get("setup_manual_fallback") is True
    assert any(task["type"] == "device_screen_prepare" for task in farm.tasks)
    farm.tasks.clear()
    service._launch_add_esim(21)
    inspects = [t for t in farm.tasks if t["type"] == "setup_session_inspect"]
    assert inspects
    assert inspects[-1]["payload"].get("recover") is False
    autos = [t for t in farm.tasks if t["type"] == "esim_auto_activation"]
    assert autos == []
    tapped = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 100, "y": 200})
    assert tapped.http_status == 200
    home = service.control_session(CUSTOMER_A, rental, {"action": "home"})
    assert home.http_status == 200
