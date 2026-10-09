"""Phase 1: eSIM setup-mode control is backend-enforced on Slot 2."""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.farm_agent_tasks import FarmTaskRequest, execute_farm_task
from application.farm_task_executor import FarmTaskExecutorDeps
from application.in_app_control_policy import decide_control
from application.setup_activity_guard import RECOVER_INTENT_ESIM
from application.vps_farm_management_service import DEVICE_CLEANUP_DETAIL
from tests.fakes_supabase import MemoryTenant
from tests.test_farm_agent_tasks import FakeRunner
from tests.test_remote_access_poc import (
    CUSTOMER_A,
    CUSTOMER_B,
    FakeFarm,
    FakePlatform,
    SLOT2_SERIAL,
    _rental,
    _service,
)


def _slot2_service(tmp_path: Path, *, farm: FakeFarm | None = None, platform: FakePlatform | None = None):
    tenant = MemoryTenant()
    platform = platform or FakePlatform(registered={SLOT2_SERIAL})
    farm = farm or FakeFarm()
    service, store, _ = _service(
        tmp_path,
        tenant=tenant,
        platform=platform,
        farm=farm,
        allowed=(2,),
        slot_map={2: SLOT2_SERIAL},
        workspace_map={2: "ws-2"},
        gads_slot_ids=(2,),
        prepare_slot_ids=(2,),
        observe_slot_ids=(2,),
    )
    rental = _rental(tenant, bay=2, user_id=CUSTOMER_A)
    return tenant, platform, farm, service, store, rental


def _mark_ready(store, rental: str) -> None:
    store.set_activation_observed(rental, "confirmed")
    session = store.get(rental)
    store.upsert(replace(session, setup_phase="complete", setup_complete=True))


def _assert_no_internals(body: dict) -> None:
    blob = json.dumps(body)
    for marker in (
        SLOT2_SERIAL,
        "workspace_id",
        "NexusLauncher",
        "MobileNetwork",
        "MANAGE_ALL_SIM",
        "one-time-secret",
        "activity_unknown",
        "left_setup",
    ):
        assert marker not in blob, marker


def test_setup_mode_blocks_home_recents_shade_and_leave_attempts(tmp_path: Path):
    _tenant, platform, farm, service, _store, rental = _slot2_service(tmp_path)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    home = service.control_session(CUSTOMER_A, rental, {"action": "home"})
    recents = service.control_session(CUSTOMER_A, rental, {"action": "recents"})
    shade = service.control_session(
        CUSTOMER_A, rental, {"action": "swipe", "x": 10, "y": 5, "x2": 10, "y2": 400}
    )
    assert home.http_status == 403 and recents.http_status == 403 and shade.http_status == 403
    assert home.body["error"] == "forbidden_control"
    assert recents.body["error"] == "forbidden_control"
    assert shade.body["error"] == "forbidden_control"
    _assert_no_internals(home.body)
    assert not any(c[0] == "home" for c in platform.calls)
    assert not any(c[0] == "recents" for c in platform.calls)
    assert any(t["type"] == "setup_session_inspect" and t["payload"].get("recover") for t in farm.tasks)


def test_setup_mode_blocks_launcher_unrelated_settings_apps_and_unknown(tmp_path: Path):
    farm = FakeFarm()
    farm.inspect_allowed = False
    farm.inspect_activity = "com.google.android.apps.nexuslauncher/.NexusLauncherActivity"
    _tenant, platform, farm, service, _store, rental = _slot2_service(tmp_path, farm=farm)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    taps_before = [c for c in platform.calls if c[0] == "tap"]
    blocked = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 40, "y": 80})
    assert blocked.http_status == 403
    assert blocked.body["error"] == "setup_state_blocked"
    _assert_no_internals(blocked.body)
    assert [c for c in platform.calls if c[0] == "tap"] == taps_before

    farm.inspect_activity = "com.android.settings/.Settings"
    settings = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 41, "y": 81})
    assert settings.http_status == 403 and settings.body["error"] == "setup_state_blocked"

    farm.inspect_activity = "com.android.chrome/.MainActivity"
    app = service.control_session(
        CUSTOMER_A, rental, {"action": "swipe", "x": 10, "y": 400, "x2": 10, "y2": 200}
    )
    assert app.http_status == 403 and app.body["error"] == "setup_state_blocked"
    assert not any(c[0] == "swipe" for c in platform.calls)

    farm.inspect_unknown = True
    unknown = service.control_session(CUSTOMER_A, rental, {"action": "type", "text": "hi"})
    assert unknown.http_status == 403 and unknown.body["error"] == "setup_state_blocked"
    _assert_no_internals(unknown.body)
    assert not any(c[0] == "type" for c in platform.calls)


def test_setup_mode_allows_esim_settings_interaction(tmp_path: Path):
    platform = FakePlatform(registered={SLOT2_SERIAL})
    farm = FakeFarm()
    farm.inspect_allowed = True
    _tenant, platform, farm, service, _store, rental = _slot2_service(
        tmp_path, farm=farm, platform=platform
    )
    created = service.create_remote_access(CUSTOMER_A, None, rental)
    assert created.http_status == 201
    inspects = [t for t in farm.tasks if t["type"] == "setup_session_inspect"]
    assert inspects and inspects[0]["payload"].get("recover") is True
    assert inspects[0]["payload"].get("phase") == "esim"
    assert inspects[0]["slot"] == 2

    tap = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 120, "y": 400})
    swipe = service.control_session(
        CUSTOMER_A, rental, {"action": "swipe", "x": 100, "y": 900, "x2": 100, "y2": 500}
    )
    typed = service.control_session(CUSTOMER_A, rental, {"action": "type", "text": "ok"})
    back = service.control_session(CUSTOMER_A, rental, {"action": "back"})
    assert tap.http_status == 200 and swipe.http_status == 200
    assert typed.http_status == 200 and back.http_status == 200
    assert {c[0] for c in platform.calls} >= {"tap", "swipe", "type", "back"}
    assert not any(c[0] in {"home", "recents"} for c in platform.calls)


def test_leave_setup_does_not_forward_and_recovers_with_sim_profiles_intent():
    runner = FakeRunner()
    runner.dumpsys_activity = (
        "mResumedActivity: ActivityRecord{abc u0 "
        "com.google.android.apps.nexuslauncher/.NexusLauncherActivity t1}"
    )
    result = execute_farm_task(
        adb_path="adb",
        slot_map={2: SLOT2_SERIAL},
        request=FarmTaskRequest(
            job_id="job-recover",
            task_type="setup_session_inspect",
            farm_slot_id=2,
            payload={"phase": "esim", "recover": True},
        ),
        deps=FarmTaskExecutorDeps(command_runner=runner),
    )
    assert result.ok is True
    assert result.details["allowed"] is False
    assert result.details["recovered"] is True
    assert any(
        call[1][:4] == ["shell", "am", "start", "-a"] and call[1][4] == RECOVER_INTENT_ESIM
        for call in runner.calls
    )
    joined = " ".join(" ".join(call[1]) for call in runner.calls)
    assert "factory" not in joined.lower()
    assert "wipe" not in joined
    assert "euicc" not in joined.lower()
    assert "provision" not in joined.lower()
    assert result.details.get("factory_reset") is None
    assert result.details.get("esim_deleted") is None


def test_ready_mode_preserves_home_and_recents(tmp_path: Path):
    platform = FakePlatform(registered={SLOT2_SERIAL})
    farm = FakeFarm()
    _tenant, platform, farm, service, store, rental = _slot2_service(
        tmp_path, farm=farm, platform=platform
    )
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    inspects_after_create = [t for t in farm.tasks if t["type"] == "setup_session_inspect"]
    _mark_ready(store, rental)
    farm.inspect_allowed = False
    home = service.control_session(CUSTOMER_A, rental, {"action": "home"})
    recents = service.control_session(CUSTOMER_A, rental, {"action": "recents"})
    assert home.http_status == 200 and recents.http_status == 200
    assert {c[0] for c in platform.calls} >= {"home", "recents"}
    inspects_after_ready = [t for t in farm.tasks if t["type"] == "setup_session_inspect"]
    assert inspects_after_ready == inspects_after_create


def test_voidfix_phase_is_not_esim_setup_mode(tmp_path: Path):
    platform = FakePlatform(registered={SLOT2_SERIAL})
    farm = FakeFarm()
    _tenant, platform, farm, service, store, rental = _slot2_service(
        tmp_path, farm=farm, platform=platform
    )
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    session = store.get(rental)
    store.upsert(replace(session, setup_phase="voidfix", setup_complete=False))
    inspects_before = [t for t in farm.tasks if t["type"] == "setup_session_inspect"]
    home = service.control_session(CUSTOMER_A, rental, {"action": "home"})
    recents = service.control_session(CUSTOMER_A, rental, {"action": "recents"})
    assert home.http_status == 200 and recents.http_status == 200
    inspects_after = [t for t in farm.tasks if t["type"] == "setup_session_inspect"]
    assert inspects_after == inspects_before
    assert not any(t["type"] == "setup_session_voidfix_cycle" for t in farm.tasks)


def test_stream_independent_of_setup_guard_and_native_resolution_unchanged(tmp_path: Path):
    farm = FakeFarm()
    farm.inspect_allowed = False
    _tenant, platform, farm, service, _store, rental = _slot2_service(tmp_path, farm=farm)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    inspects_after_create = [t for t in farm.tasks if t["type"] == "setup_session_inspect"]
    stream = service.open_stream(CUSTOMER_A, rental)
    assert getattr(stream, "device_id") == SLOT2_SERIAL
    inspects_after_stream = [t for t in farm.tasks if t["type"] == "setup_session_inspect"]
    assert inspects_after_stream == inspects_after_create
    status = service.device_status_for_customer(CUSTOMER_A, rental)
    assert status.http_status == 200
    assert status.body["native_resolution"] == {"width": 1440, "height": 3120}
    assert status.body["coordinate_space"] == "native_device_pixels"
    assert [t["type"] for t in farm.tasks if t["type"] == "device_display_size"]


def test_setup_mode_preserves_ownership_voidfix_cleanup_and_fail_closed(tmp_path: Path):
    tenant, platform, farm, service, store, rental = _slot2_service(tmp_path)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    stolen = service.control_session(CUSTOMER_B, rental, {"action": "tap", "x": 1, "y": 1})
    assert stolen.http_status == 403
    assert stolen.body["error"] == "rental_not_owned"
    assert "no_factory_reset" in DEVICE_CLEANUP_DETAIL
    assert "no_silent_esim_delete" in DEVICE_CLEANUP_DETAIL
    done = service.complete_setup(CUSTOMER_A, rental)
    assert done.http_status == 200
    assert done.body["esim_deleted"] is False
    assert done.body["factory_reset"] is False
    assert any(t["type"] == "setup_session_safe_cleanup" for t in farm.tasks)
    assert not any(t["type"] == "setup_session_voidfix_cycle" for t in farm.tasks)
    inspect_payloads = [t["payload"] for t in farm.tasks if t["type"] == "setup_session_inspect"]
    assert inspect_payloads
    assert all("factory" not in json.dumps(p).lower() for p in inspect_payloads)
    source = (ROOT / "application" / "remote_access_service.py").read_text(encoding="utf-8")
    assert "EuiccManager(" not in source
    assert "lock-task" not in source.lower()
    _ = platform
    _ = store


def test_inspect_failure_fail_closes_control_without_breaking_create(tmp_path: Path):
    farm = FakeFarm()
    farm.fail_types.add("setup_session_inspect")
    platform = FakePlatform(registered={SLOT2_SERIAL})
    _tenant, platform, farm, service, _store, rental = _slot2_service(
        tmp_path, farm=farm, platform=platform
    )
    created = service.create_remote_access(CUSTOMER_A, None, rental)
    assert created.http_status == 201
    tap = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 40, "y": 80})
    assert tap.http_status == 403
    assert tap.body["error"] == "setup_state_blocked"
    assert not any(c[0] == "tap" for c in platform.calls)


def test_decide_control_setup_mode_keeps_public_payloads():
    assert decide_control({"action": "home"}).allowed is True
    assert decide_control({"action": "recents"}).allowed is True
    denied = decide_control({"action": "home"}, setup_mode=True)
    assert denied.allowed is False and denied.reason == "forbidden_control"
    assert decide_control({"action": "recents"}, setup_mode=True).allowed is False
    assert decide_control({"action": "tap", "x": 1, "y": 2}, setup_mode=True).allowed is True
    assert decide_control({"action": "back"}, setup_mode=True).allowed is True
    assert decide_control(
        {"action": "swipe", "x": 10, "y": 400, "x2": 10, "y2": 200}, setup_mode=True
    ).allowed is True
