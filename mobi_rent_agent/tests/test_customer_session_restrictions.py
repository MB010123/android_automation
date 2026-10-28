"""Customer rental Settings/Back/VoidFix restrictions without a kiosk loop."""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.in_app_control_policy import public_allowed_controls
from application.session_restriction_policy import (
    CAMERA_PACKAGES,
    COMPANION_PACKAGE,
    GADS_SETTINGS_PACKAGE,
)
from application.setup_activity_guard import RECOVER_INTENT_ESIM
from application.vps_farm_management_service import DEVICE_CLEANUP_DETAIL
from tests.fakes_supabase import MemoryTenant
from tests.test_esim_setup_mode import _assert_no_internals, _recover_inspects, _slot2_service
from tests.test_remote_access_poc import (
    CUSTOMER_A,
    CUSTOMER_B,
    FakeFarm,
    FakePlatform,
    PNG_BYTES,
    SLOT2_SERIAL,
    _rental,
    _service,
)


def _created(tmp_path: Path, *, farm: FakeFarm | None = None, voidfix_package: str | None = None):
    farm = farm or FakeFarm()
    tenant, platform, farm, service, store, rental = _slot2_service(tmp_path, farm=farm)
    if voidfix_package:
        service._voidfix_package = voidfix_package
    created = service.create_remote_access(CUSTOMER_A, None, rental)
    assert created.http_status == 201
    return tenant, platform, farm, service, store, rental


def test_customer_remote_access_always_flips_phone_to_client_mode(tmp_path: Path):
    """Customer open/stream => Farm `device_access_mode admin=false`; never admin=true."""
    _tenant, _platform, farm, service, _store, rental = _created(tmp_path)
    flips = [t for t in farm.tasks if t["type"] == "device_access_mode"]
    assert flips and flips[-1]["slot"] == 2
    assert all(t["payload"] == {"admin": False} for t in flips)
    created_again = service.create_remote_access(CUSTOMER_A, None, rental)
    assert created_again.http_status == 200
    assert created_again.body["admin"] is False
    fetched = service.get_remote_access(CUSTOMER_A, None, rental)
    assert fetched.http_status == 200
    assert fetched.body["admin"] is False
    before = len([t for t in farm.tasks if t["type"] == "device_access_mode"])
    stream = service.open_stream(CUSTOMER_A, rental)
    assert getattr(stream, "device_id") == SLOT2_SERIAL
    flips = [t for t in farm.tasks if t["type"] == "device_access_mode"]
    assert len(flips) == before + 1
    assert all(t["payload"] == {"admin": False} for t in flips)
    # Customer `control` with admin=true is clamped to client mode.
    clamped = service.control_session(CUSTOMER_A, rental, {"admin": True})
    assert clamped.http_status == 200
    assert clamped.body["admin"] is False
    assert not any(t["payload"].get("admin") is True for t in farm.tasks)


def test_settings_redirects_to_add_esim_not_homepage(tmp_path: Path):
    farm = FakeFarm()
    farm.inspect_activity = "com.google.android.apps.nexuslauncher/.NexusLauncherActivity"
    _tenant, platform, farm, service, _store, rental = _created(tmp_path, farm=farm)
    semantic = service.control_session(CUSTOMER_A, rental, {"action": "settings"})
    assert semantic.http_status == 200
    assert semantic.body["restricted_destination"] == "add_esim"
    assert semantic.body["restriction"] == "settings_redirected"
    assert semantic.body["forwarded"] is True
    _assert_no_internals(semantic.body)
    recoveries = _recover_inspects(farm)
    assert recoveries
    assert recoveries[0]["payload"]["recover"] is True
    assert recoveries[0]["slot"] == 2
    farm.inspect_activity = "com.android.settings/.Settings"
    tapped = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 40, "y": 80})
    assert tapped.http_status == 200
    assert tapped.body["restricted_destination"] == "add_esim"
    assert any(c[0] == "tap" for c in platform.calls)
    assert any(c[0] == "home" for c in platform.calls)
    assert not any(c[0] == "revoke" for c in platform.calls)
    stream = service.open_stream(CUSTOMER_A, rental)
    assert getattr(stream, "device_id") == SLOT2_SERIAL


def test_unrelated_settings_blocked_without_dropping_stream(tmp_path: Path):
    farm = FakeFarm()
    _tenant, platform, farm, service, _store, rental = _created(tmp_path, farm=farm)
    for activity in (
        "com.android.settings/.Settings$SecurityDashboardActivity",
        "com.android.settings/.Settings$AccountDashboardActivity",
        "com.android.settings/.Settings$DevelopmentSettingsDashboardActivity",
    ):
        farm.inspect_activity = activity
        result = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 10, "y": 10})
        assert result.http_status == 200, activity
        assert result.body["restricted_destination"] == "add_esim"
        _assert_no_internals(result.body)
    stream = service.open_stream(CUSTOMER_A, rental)
    assert getattr(stream, "device_id") == SLOT2_SERIAL
    assert not any(c[0] in {"revoke", "release"} for c in platform.calls)


def test_back_from_esim_root_goes_home_not_settings_homepage(tmp_path: Path):
    farm = FakeFarm()
    farm.inspect_activity = "com.android.settings/.network.telephony.MobileNetworkActivity"
    farm.inspect_allowed = True
    _tenant, platform, farm, service, _store, rental = _created(tmp_path, farm=farm)
    back = service.control_session(CUSTOMER_A, rental, {"action": "back"})
    assert back.http_status == 200
    assert back.body["restricted_destination"] == "home"
    assert back.body["restriction"] == "esim_back_to_home"
    assert any(c[0] == "home" for c in platform.calls)
    assert not any(c[0] == "back" for c in platform.calls)
    assert "Settings" not in json.dumps(back.body)
    assert _recover_inspects(farm) == []
    farm.inspect_activity = "com.google.android.euicc/.ui.ConfirmDownloadActivity"
    nested = service.control_session(CUSTOMER_A, rental, {"action": "back"})
    assert nested.http_status == 200
    assert any(c[0] == "back" for c in platform.calls)


def test_voidfix_blocked_in_customer_session_without_killing_gads(tmp_path: Path):
    farm = FakeFarm()
    farm.inspect_activity = "org.voidfix.smsgateway/.SmsGatewayActivity"
    _tenant, platform, farm, service, _store, rental = _created(
        tmp_path, farm=farm, voidfix_package="org.voidfix.smsgateway"
    )
    blocked = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 20, "y": 20})
    assert blocked.http_status == 200
    assert blocked.body["restricted_destination"] == "home"
    assert blocked.body["restriction"] == "sensitive_app_blocked"
    assert any(c[0] == "home" for c in platform.calls)
    assert not any(c[0] in {"revoke", "release"} for c in platform.calls)
    assert not any(t["type"] == "setup_session_voidfix_cycle" for t in farm.tasks)
    stream = service.open_stream(CUSTOMER_A, rental)
    assert getattr(stream, "device_id") == SLOT2_SERIAL


def test_gads_settings_and_companion_blocked_without_killing_remote_access(tmp_path: Path):
    farm = FakeFarm()
    _tenant, platform, farm, service, _store, rental = _created(tmp_path, farm=farm)
    for activity in (
        f"{GADS_SETTINGS_PACKAGE}/.MainActivity",
        f"{GADS_SETTINGS_PACKAGE}/com.shamanec.settings.MainActivity",
        f"{COMPANION_PACKAGE}/.MainActivity",
        f"{COMPANION_PACKAGE}/com.mobirent.companion.MainActivity",
    ):
        farm.inspect_activity = activity
        platform.calls.clear()
        blocked = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 20, "y": 20})
        assert blocked.http_status == 200, activity
        assert blocked.body["restricted_destination"] == "home"
        assert blocked.body["restriction"] == "sensitive_app_blocked"
        homes = [c for c in platform.calls if c[0] == "home"]
        assert len(homes) == 1, activity
        assert not any(c[0] in {"revoke", "release"} for c in platform.calls)
    farm.inspect_activity = "com.google.android.GoogleCamera/.CameraActivity"
    platform.calls.clear()
    camera = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 22, "y": 22})
    assert camera.http_status == 200
    assert camera.body["restricted_destination"] == "home"
    assert camera.body["restriction"] == "sensitive_app_blocked"
    assert len([c for c in platform.calls if c[0] == "home"]) == 1
    farm.inspect_activity = "com.google.android.apps.nexuslauncher/.NexusLauncherActivity"
    platform.calls.clear()
    allowed = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 21, "y": 21})
    assert allowed.http_status == 200
    assert "restricted_destination" not in allowed.body
    assert not any(c[0] == "home" for c in platform.calls)
    stream = service.open_stream(CUSTOMER_A, rental)
    assert getattr(stream, "device_id") == SLOT2_SERIAL


def test_gads_remote_access_surfaces_are_not_sent_home(tmp_path: Path):
    farm = FakeFarm()
    _tenant, platform, farm, service, _store, rental = _created(tmp_path, farm=farm)
    for activity, action, body in (
        (
            f"{GADS_SETTINGS_PACKAGE}/com.shamanec.settings.streaming.MjpegScreenCaptureActivity",
            "tap",
            {"action": "tap", "x": 5, "y": 5},
        ),
        (
            f"{GADS_SETTINGS_PACKAGE}/com.shamanec.settings.audio.AudioProjectionActivity",
            "swipe",
            {"action": "swipe", "x": 10, "y": 400, "x2": 10, "y2": 200},
        ),
        (
            f"{GADS_SETTINGS_PACKAGE}/com.shamanec.settings.RemoteKeyboardIME",
            "rotate",
            {"action": "rotate", "orientation": "landscape"},
        ),
    ):
        farm.inspect_activity = activity
        platform.calls.clear()
        result = service.control_session(CUSTOMER_A, rental, body)
        assert result.http_status == 200, activity
        assert "restricted_destination" not in result.body
        assert any(c[0] == action for c in platform.calls), activity
        assert not any(c[0] == "home" for c in platform.calls)
        assert not any(c[0] in {"revoke", "release"} for c in platform.calls)
    stream = service.open_stream(CUSTOMER_A, rental)
    assert getattr(stream, "device_id") == SLOT2_SERIAL


def test_shade_and_quick_settings_blocked_without_breaking_gads(tmp_path: Path):
    farm = FakeFarm()
    farm.inspect_activity = "com.google.android.apps.nexuslauncher/.NexusLauncherActivity"
    _tenant, platform, farm, service, _store, rental = _created(tmp_path, farm=farm)
    farm.inspect_activity = "com.android.systemui/.shade.NotificationShadeWindow"
    visible = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 20, "y": 20})
    assert visible.body["restriction"] == "shade_blocked"
    assert visible.body["restricted_destination"] == "home"
    farm.inspect_activity = "com.android.systemui/.qs.QSPanel"
    qs_ui = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 21, "y": 21})
    assert qs_ui.body["restriction"] == "shade_blocked"
    stream = service.open_stream(CUSTOMER_A, rental)
    assert getattr(stream, "device_id") == SLOT2_SERIAL
    assert not any(c[0] in {"revoke", "release"} for c in platform.calls)


def test_normal_apps_and_esim_screens_unchanged_with_operator_ui_blocks(tmp_path: Path):
    farm = FakeFarm()
    _tenant, platform, farm, service, _store, rental = _created(tmp_path, farm=farm)
    farm.inspect_activity = "com.google.android.dialer/.GoogleDialtactsActivity"
    chrome = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 15, "y": 15})
    assert chrome.http_status == 200
    assert "restricted_destination" not in chrome.body
    assert any(c[0] == "tap" for c in platform.calls)
    farm.inspect_activity = "com.android.settings/.network.telephony.MobileNetworkActivity"
    farm.inspect_allowed = True
    platform.calls.clear()
    esim = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 16, "y": 16})
    assert esim.http_status == 200
    assert "restricted_destination" not in esim.body
    assert any(c[0] == "tap" for c in platform.calls)
    assert not any(c[0] == "home" for c in platform.calls)
    stream = service.open_stream(CUSTOMER_A, rental)
    assert getattr(stream, "device_id") == SLOT2_SERIAL


def test_samsung_esim_gallery_and_picker_taps_are_not_redirected(tmp_path: Path):
    farm = FakeFarm()
    _tenant, platform, farm, service, _store, rental = _created(tmp_path, farm=farm)
    for activity in (
        "com.samsung.android.app.telephonyui/.netsettings.ui.simcardmanager.SimCardMgrActivity",
        "com.samsung.android.euicc/.EuiccActivity",
        "com.sec.android.gallery3d/.GalleryActivity",
        "com.google.android.documentsui/.picker.PickActivity",
        "com.samsung.android.biometrics.app.setting/.FingerprintActivity",
        "com.android.settings/.network.telephony.MobileNetworkActivity",
        "com.android.settings/.spa.SpaActivity",
        "com.android.settings/.Settings$NetworkDashboardActivity",
    ):
        farm.inspect_activity = activity
        farm.inspect_allowed = True
        platform.calls.clear()
        before = len(farm.tasks)
        tapped = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 40, "y": 80})
        assert tapped.http_status == 200, activity
        assert "restricted_destination" not in tapped.body, activity
        assert any(c[0] == "tap" for c in platform.calls)
        assert not any(c[0] == "home" for c in platform.calls)
        assert not any(
            t["type"] == "setup_session_inspect" and t["payload"].get("recover")
            for t in farm.tasks[before:]
        )


def test_package_installer_does_not_disconnect_stream(tmp_path: Path):
    farm = FakeFarm()
    farm.inspect_activity = "com.android.packageinstaller/.InstallStart"
    _tenant, platform, farm, service, _store, rental = _created(tmp_path, farm=farm)
    blocked = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 8, "y": 8})
    assert blocked.http_status == 200
    assert blocked.body["restricted_destination"] == "home"
    stream = service.open_stream(CUSTOMER_A, rental)
    assert getattr(stream, "device_id") == SLOT2_SERIAL
    assert not any(c[0] in {"revoke", "release"} for c in platform.calls)


def test_qr_upload_tap_swipe_still_work(tmp_path: Path):
    farm = FakeFarm()
    farm.inspect_activity = "com.google.android.apps.nexuslauncher/.NexusLauncherActivity"
    _tenant, platform, farm, service, store, rental = _created(tmp_path, farm=farm)
    tap = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 40, "y": 80})
    swipe = service.control_session(
        CUSTOMER_A, rental, {"action": "swipe", "x": 10, "y": 400, "x2": 10, "y2": 200}
    )
    typed = service.control_session(CUSTOMER_A, rental, {"action": "type", "text": "ok"})
    assert tap.http_status == 200 and swipe.http_status == 200 and typed.http_status == 200
    assert {c[0] for c in platform.calls} >= {"tap", "swipe", "type"}
    assert "restricted_destination" not in tap.body
    source = (ROOT / "application" / "esim_qr_upload.py").read_text(encoding="utf-8")
    assert "qr_image" in source


def test_ownership_isolation_rejects_package_targeting(tmp_path: Path):
    farm = FakeFarm()
    tenant, platform, farm, service, _store, rental = _created(tmp_path, farm=farm)
    stolen = service.control_session(CUSTOMER_B, rental, {"action": "settings"})
    assert stolen.http_status == 403
    assert stolen.body["error"] == "rental_not_owned"
    for key, value in (
        ("package", "org.voidfix.smsgateway"),
        ("intent", "android.settings.SETTINGS"),
        ("am", "start"),
        ("serial", SLOT2_SERIAL),
    ):
        denied = service.control_session(CUSTOMER_A, rental, {"action": "settings", key: value})
        assert denied.http_status == 403, key
        assert denied.body["error"] == "forbidden_control"
    assert not any(t["payload"].get("recover") for t in farm.tasks if t["type"] == "setup_session_inspect")
    _ = tenant
    _ = platform


def test_cancel_revokes_session_without_esim_delete(tmp_path: Path):
    farm = FakeFarm()
    _tenant, platform, farm, service, _store, rental = _created(tmp_path, farm=farm)
    done = service.complete_setup(CUSTOMER_A, rental)
    assert done.http_status == 200
    assert done.body["esim_deleted"] is False
    assert done.body["factory_reset"] is False
    assert any(t["type"] == "setup_session_safe_cleanup" for t in farm.tasks)
    assert "no_silent_esim_delete" in DEVICE_CLEANUP_DETAIL
    assert any(c[0] == "revoke" for c in platform.calls)


def test_reconnect_still_501_phone_ready_observational_full_nav_allowed(tmp_path: Path):
    farm = FakeFarm()
    tenant, platform, farm, service, store, rental = _created(tmp_path, farm=farm)
    reconnect = service.reconnect_cellular_for_customer(CUSTOMER_A, rental, {})
    assert reconnect.http_status == 200
    assert reconnect.body["ok"] is True
    store.set_activation_observed(rental, "confirmed")
    store.upsert(replace(store.get(rental), setup_phase="complete", setup_complete=True))
    got = service.get_remote_access(CUSTOMER_A, None, rental)
    assert got.body["setup_mode"] is False
    assert got.body["ui_state"] == "phone_ready"
    assert got.body["allowed_controls"] == public_allowed_controls(session_restrictions=True)
    assert "notification_shade" not in got.body["allowed_controls"]
    assert "quick_settings" not in got.body["allowed_controls"]
    for action in ("home", "recents"):
        result = service.control_session(CUSTOMER_A, rental, {"action": action})
        assert result.http_status == 200, action
    shade = service.control_session(CUSTOMER_A, rental, {"action": "notification_shade"})
    qs = service.control_session(CUSTOMER_A, rental, {"action": "quick_settings"})
    assert shade.http_status == 200 and qs.http_status == 200
    assert shade.body["restriction"] == "shade_blocked"
    assert qs.body["restriction"] == "shade_blocked"
    assert {c[0] for c in platform.calls} >= {"home", "recents"}
    assert "notification_shade" not in {c[0] for c in platform.calls}
    assert "quick_settings" not in {c[0] for c in platform.calls}
    _ = tenant


def test_no_recover_loop_when_leaving_esim(tmp_path: Path):
    farm = FakeFarm()
    farm.inspect_activity = "com.google.android.apps.nexuslauncher/.NexusLauncherActivity"
    _tenant, platform, farm, service, _store, rental = _created(tmp_path, farm=farm)
    home = service.control_session(CUSTOMER_A, rental, {"action": "home"})
    tap = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 12, "y": 12})
    assert home.http_status == 200 and tap.http_status == 200
    assert "restricted_destination" not in tap.body
    assert _recover_inspects(farm) == []
    inspects = [t for t in farm.tasks if t["type"] == "setup_session_inspect"]
    assert inspects
    assert all(t["payload"].get("recover") is False for t in inspects)
    assert not any(c[0] == "revoke" for c in platform.calls)


def test_restrictions_off_restores_open_settings(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    farm.inspect_activity = "com.android.settings/.Settings"
    platform = FakePlatform(registered={SLOT2_SERIAL})
    service, _store, _clock = _service(
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
        session_restrictions=False,
    )
    rental = _rental(tenant, bay=2, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    tap = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 1, "y": 1})
    settings = service.control_session(CUSTOMER_A, rental, {"action": "settings"})
    home = service.control_session(CUSTOMER_A, rental, {"action": "home"})
    recents = service.control_session(CUSTOMER_A, rental, {"action": "recents"})
    assert tap.http_status == 200
    assert settings.http_status == 200
    assert home.http_status == 200 and recents.http_status == 200
    assert "restricted_destination" not in tap.body
    assert "restricted_destination" not in settings.body
    farm.inspect_activity = "org.voidfix.smsgateway/.SmsGatewayActivity"
    voidfix = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 2, "y": 2})
    assert voidfix.http_status == 200
    assert "restricted_destination" not in voidfix.body
    farm.inspect_activity = f"{GADS_SETTINGS_PACKAGE}/.MainActivity"
    gads_settings = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 3, "y": 3})
    assert gads_settings.http_status == 200
    assert "restricted_destination" not in gads_settings.body
    farm.inspect_activity = f"{COMPANION_PACKAGE}/.MainActivity"
    companion = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 4, "y": 4})
    assert companion.http_status == 200
    assert "restricted_destination" not in companion.body
    farm.inspect_activity = "com.google.android.GoogleCamera/.CameraActivity"
    camera = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 5, "y": 5})
    assert camera.http_status == 200
    assert "restricted_destination" not in camera.body
    shade = service.control_session(CUSTOMER_A, rental, {"action": "notification_shade"})
    qs = service.control_session(CUSTOMER_A, rental, {"action": "quick_settings"})
    assert shade.http_status == 200 and qs.http_status == 200
    assert "restricted_destination" not in shade.body
    assert "restricted_destination" not in qs.body
    uploaded = service.upload_esim_qr(CUSTOMER_A, rental, PNG_BYTES)
    assert uploaded.http_status == 200
    assert uploaded.body["placed"] is True
    assert "restricted_destination" not in uploaded.body
    assert _recover_inspects(farm) == []
    assert not any(c[0] in {"revoke", "release"} for c in platform.calls)


def test_source_does_not_restart_gads_or_use_lock_task():
    source = (ROOT / "application" / "remote_access_service.py").read_text(encoding="utf-8")
    assert "lock-task" not in source.lower()
    assert "device owner" not in source.lower()
    assert "force-stop" not in source
    assert "pm disable" not in source
    assert "pm hide" not in source
    assert "pm uninstall" not in source
    assert "_launch_add_esim" in source
    assert "_navigate_rental_to_add_esim" in source
    assert "recover=True" in source
    policy = (ROOT / "application" / "session_restriction_policy.py").read_text(encoding="utf-8")
    assert GADS_SETTINGS_PACKAGE in policy
    assert COMPANION_PACKAGE in policy
    assert "com.google.android.GoogleCamera" in CAMERA_PACKAGES
    assert "force-stop" not in policy
    assert "pm disable" not in policy
    assert "pm hide" not in policy
    assert "pm clear" not in policy
    guard = (ROOT / "application" / "setup_activity_guard.py").read_text(encoding="utf-8")
    assert RECOVER_INTENT_ESIM in guard
    farm_source = (ROOT / "application" / "setup_session_farm_task.py").read_text(encoding="utf-8")
    assert "am" in farm_source and "start" in farm_source and "recover_intent" in farm_source


def _all_bay_maps() -> tuple[dict[int, str], dict[int, str]]:
    slot_map = {bay: f"SERIAL-BAY-{bay:02d}" for bay in range(1, 21)}
    workspace_map = {bay: f"ws-{bay}" for bay in range(1, 21)}
    return slot_map, workspace_map


def _twenty_bay_service(tmp_path: Path, *, farm: FakeFarm | None = None, session_restrictions: bool = True):
    slot_map, workspace_map = _all_bay_maps()
    tenant = MemoryTenant()
    platform = FakePlatform(registered=set(slot_map.values()))
    farm = farm or FakeFarm()
    bays = tuple(range(1, 21))
    service, store, _clock = _service(
        tmp_path,
        tenant=tenant,
        platform=platform,
        farm=farm,
        allowed=bays,
        slot_map=slot_map,
        workspace_map=workspace_map,
        gads_slot_ids=bays,
        prepare_slot_ids=bays,
        observe_slot_ids=bays,
        session_restrictions=session_restrictions,
    )
    return tenant, platform, farm, service, store, slot_map


def test_unrelated_wifi_apps_system_settings_redirect_to_add_esim(tmp_path: Path):
    farm = FakeFarm()
    _tenant, platform, farm, service, _store, rental = _created(tmp_path, farm=farm)
    for activity in (
        "com.android.settings/.wifi.WifiSettings",
        "com.android.settings/.applications.ManageApplications",
        "com.android.settings/.system.SystemDashboardFragment",
    ):
        farm.inspect_activity = activity
        result = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 11, "y": 11})
        assert result.http_status == 200, activity
        assert result.body["restricted_destination"] == "add_esim"
        _assert_no_internals(result.body)
    assert not any(c[0] in {"revoke", "release"} for c in platform.calls)


def test_nested_esim_back_is_real_back_not_home(tmp_path: Path):
    farm = FakeFarm()
    farm.inspect_activity = "com.google.android.euicc/.ui.EuiccProvisioningActivity"
    _tenant, platform, farm, service, _store, rental = _created(tmp_path, farm=farm)
    nested = service.control_session(CUSTOMER_A, rental, {"action": "back"})
    assert nested.http_status == 200
    assert "restricted_destination" not in nested.body
    assert any(c[0] == "back" for c in platform.calls)
    assert not any(c[0] == "home" for c in platform.calls)
    assert _recover_inspects(farm) == []


def test_home_recents_rotate_stay_allowed_shade_qs_blocked(tmp_path: Path):
    farm = FakeFarm()
    farm.inspect_activity = "com.google.android.apps.nexuslauncher/.NexusLauncherActivity"
    _tenant, platform, farm, service, _store, rental = _created(tmp_path, farm=farm)
    for action, body in (
        ("home", {"action": "home"}),
        ("recents", {"action": "recents"}),
        ("rotate", {"action": "rotate", "orientation": "landscape"}),
    ):
        result = service.control_session(CUSTOMER_A, rental, body)
        assert result.http_status == 200, action
        assert "restricted_destination" not in result.body
    shade = service.control_session(CUSTOMER_A, rental, {"action": "notification_shade"})
    qs = service.control_session(CUSTOMER_A, rental, {"action": "quick_settings"})
    top = service.control_session(
        CUSTOMER_A, rental, {"action": "swipe", "x": 100, "y": 8, "x2": 100, "y2": 400}
    )
    assert shade.body["restriction"] == "shade_blocked"
    assert qs.body["restriction"] == "shade_blocked"
    assert top.body["restriction"] == "shade_blocked"
    assert {c[0] for c in platform.calls} >= {"home", "recents", "rotate"}
    assert "notification_shade" not in {c[0] for c in platform.calls}
    assert "quick_settings" not in {c[0] for c in platform.calls}
    assert not any(c[0] == "swipe" for c in platform.calls)
    assert _recover_inspects(farm) == []
    stream = service.open_stream(CUSTOMER_A, rental)
    assert getattr(stream, "device_id") == SLOT2_SERIAL
    assert not any(c[0] in {"revoke", "release"} for c in platform.calls)


def test_qr_upload_navigates_assigned_phone_to_add_esim(tmp_path: Path):
    farm = FakeFarm()
    farm.inspect_activity = "com.google.android.apps.nexuslauncher/.NexusLauncherActivity"
    tenant, platform, farm, service, store, rental = _created(tmp_path, farm=farm)
    uploaded = service.upload_esim_qr(CUSTOMER_A, rental, PNG_BYTES)
    assert uploaded.http_status == 200
    assert uploaded.body["placed"] is True
    assert uploaded.body["restricted_destination"] == "add_esim"
    assert uploaded.body["restriction"] == "qr_navigated_to_add_esim"
    _assert_no_internals(uploaded.body)
    assert _recover_inspects(farm) == []
    assert store.get(rental) is not None
    assert not any(c[0] in {"revoke", "release"} for c in platform.calls)
    _ = tenant


def test_qr_upload_navigate_uses_rental_slot_not_hardcoded_bay(tmp_path: Path):
    farm = FakeFarm()
    tenant, platform, farm, service, _store, slot_map = _twenty_bay_service(tmp_path, farm=farm)
    other = _rental(tenant, bay=7, user_id=CUSTOMER_B)
    stolen = service.upload_esim_qr(CUSTOMER_B, other, PNG_BYTES)
    assert stolen.http_status == 200
    assert stolen.body["restricted_destination"] == "add_esim"
    assert _recover_inspects(farm) == []
    farm.tasks.clear()
    for bay in range(1, 21):
        rental = _rental(tenant, bay=bay, user_id=CUSTOMER_A)
        uploaded = service.upload_esim_qr(CUSTOMER_A, rental, PNG_BYTES)
        assert uploaded.http_status == 200, bay
        assert uploaded.body["restricted_destination"] == "add_esim"
        assert _recover_inspects(farm) == []
        assert slot_map[bay] not in json.dumps(uploaded.body)
    assert not any(c[0] in {"revoke", "release"} for c in platform.calls)


def test_qr_upload_navigates_without_gads_session(tmp_path: Path):
    farm = FakeFarm()
    tenant, platform, farm, service, store, _slot_map = _twenty_bay_service(tmp_path, farm=farm)
    rental = _rental(tenant, bay=11, user_id=CUSTOMER_A)
    assert store.get(rental) is None
    uploaded = service.upload_esim_qr(CUSTOMER_A, rental, PNG_BYTES)
    assert uploaded.http_status == 200
    assert uploaded.body["restricted_destination"] == "add_esim"
    session = store.get(rental)
    assert session is not None
    assert session.status != "active"
    assert session.platform_secret is None
    assert _recover_inspects(farm) == []
    assert not any(c[0] in {"grant", "revoke", "release"} for c in platform.calls)
    assert "serial" not in uploaded.body


def test_qr_upload_navigate_disabled_when_flag_off(tmp_path: Path):
    farm = FakeFarm()
    tenant, _platform, farm, service, _store, _slot_map = _twenty_bay_service(
        tmp_path, farm=farm, session_restrictions=False
    )
    rental = _rental(tenant, bay=11, user_id=CUSTOMER_A)
    uploaded = service.upload_esim_qr(CUSTOMER_A, rental, PNG_BYTES)
    assert uploaded.http_status == 200
    assert uploaded.body["placed"] is True
    assert "restricted_destination" not in uploaded.body
    assert _recover_inspects(farm) == []


def test_qr_upload_still_succeeds_if_add_esim_navigate_fails(tmp_path: Path):
    farm = FakeFarm()
    farm.fail_types.add("setup_session_inspect")
    _tenant, platform, farm, service, _store, rental = _created(tmp_path, farm=farm)
    uploaded = service.upload_esim_qr(CUSTOMER_A, rental, PNG_BYTES)
    assert uploaded.http_status == 200
    assert uploaded.body["placed"] is True
    assert uploaded.body["restricted_destination"] == "add_esim"
    assert _recover_inspects(farm) == []
    assert not any(c[0] in {"revoke", "release"} for c in platform.calls)


def test_restrictions_use_authorized_rental_slot_for_all_20_bays(tmp_path: Path):
    farm = FakeFarm()
    farm.inspect_activity = "com.android.settings/.Settings"
    tenant, platform, farm, service, _store, slot_map = _twenty_bay_service(tmp_path, farm=farm)
    foreign = _rental(tenant, bay=19, user_id=CUSTOMER_B)
    assert service.create_remote_access(CUSTOMER_B, None, foreign).http_status == 201
    for bay in range(1, 21):
        if bay == 19:
            continue
        rental = _rental(tenant, bay=bay, user_id=CUSTOMER_A)
        created = service.create_remote_access(CUSTOMER_A, None, rental)
        assert created.http_status == 201, bay
        stolen = service.control_session(CUSTOMER_B, rental, {"action": "settings"})
        assert stolen.http_status == 403
        farm.tasks.clear()
        settings = service.control_session(CUSTOMER_A, rental, {"action": "settings"})
        assert settings.http_status == 200, bay
        assert settings.body["restricted_destination"] == "add_esim"
        recoveries = _recover_inspects(farm)
        assert recoveries and recoveries[0]["slot"] == bay
        assert recoveries[0]["payload"]["recover"] is True
        _assert_no_internals(settings.body)
        assert slot_map[bay] not in json.dumps(settings.body)
        other_bay = service.control_session(CUSTOMER_B, foreign, {"action": "settings"})
        assert other_bay.http_status == 200
        assert _recover_inspects(farm)[-1]["slot"] == 19
    farm.tasks.clear()
    own_nineteen = service.control_session(CUSTOMER_B, foreign, {"action": "settings"})
    assert own_nineteen.http_status == 200
    assert _recover_inspects(farm)[-1]["slot"] == 19
    assert service.control_session(CUSTOMER_A, foreign, {"action": "settings"}).http_status == 403
    assert not any(c[0] in {"revoke", "release"} for c in platform.calls)
    source = (ROOT / "application" / "remote_access_service.py").read_text(encoding="utf-8")
    assert "session.slot_id" in source
    assert "_launch_add_esim(session.slot_id)" in source
    assert "_launch_add_esim(2)" not in source
    assert "_launch_add_esim(auth.slot_id)" in source


def test_session_end_stops_restrictions_and_does_not_leak_across_rentals(tmp_path: Path):
    farm = FakeFarm()
    tenant, platform, farm, service, _store, _slot_map = _twenty_bay_service(tmp_path, farm=farm)
    first = _rental(tenant, bay=4, user_id=CUSTOMER_A)
    second = _rental(tenant, bay=15, user_id=CUSTOMER_B)
    assert service.create_remote_access(CUSTOMER_A, None, first).http_status == 201
    assert service.create_remote_access(CUSTOMER_B, None, second).http_status == 201
    done = service.complete_setup(CUSTOMER_A, first)
    assert done.http_status == 200
    assert done.body["esim_deleted"] is False
    closed = service.control_session(CUSTOMER_A, first, {"action": "settings"})
    assert closed.http_status in {403, 409}
    farm.tasks.clear()
    farm.inspect_activity = "com.android.settings/.Settings"
    other = service.control_session(CUSTOMER_B, second, {"action": "settings"})
    assert other.http_status == 200
    recoveries = _recover_inspects(farm)
    assert recoveries and recoveries[0]["slot"] == 15
    assert all(t["slot"] != 4 for t in recoveries)
    assert any(c[0] == "revoke" for c in platform.calls)
    assert not any(c[0] == "release" for c in platform.calls)


def test_restriction_paths_never_revoke_or_force_stop_gads(tmp_path: Path):
    farm = FakeFarm()
    farm.inspect_activity = "com.android.settings/.Settings"
    _tenant, platform, farm, service, _store, rental = _created(tmp_path, farm=farm)
    service.control_session(CUSTOMER_A, rental, {"action": "settings"})
    farm.inspect_activity = "org.voidfix.smsgateway/.SmsGatewayActivity"
    service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 3, "y": 3})
    farm.inspect_activity = f"{GADS_SETTINGS_PACKAGE}/.MainActivity"
    service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 3, "y": 4})
    farm.inspect_activity = f"{COMPANION_PACKAGE}/.MainActivity"
    service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 3, "y": 5})
    farm.inspect_activity = "com.android.packageinstaller/.InstallStart"
    service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 4, "y": 4})
    service.upload_esim_qr(CUSTOMER_A, rental, PNG_BYTES)
    stream = service.open_stream(CUSTOMER_A, rental)
    assert getattr(stream, "device_id") == SLOT2_SERIAL
    assert not any(c[0] in {"revoke", "release"} for c in platform.calls)
    source = (ROOT / "application" / "remote_access_service.py").read_text(encoding="utf-8")
    navigate = source.split("def _navigate_rental_to_add_esim", 1)[1].split("def ", 1)[0]
    enforce = source.split("def _enforce_foreground", 1)[1].split("def ", 1)[0]
    launch = source.split("def _launch_add_esim", 1)[1].split("def ", 1)[0]
    for fragment in (navigate, enforce, launch):
        assert "revoke_access" not in fragment
        assert "release_device" not in fragment
        assert "force-stop" not in fragment
        assert "MediaProjection" not in fragment


def test_slot18_settings_homepage_escapes_to_add_esim_not_parent_settings(tmp_path: Path):
    farm = FakeFarm()
    farm.inspect_activity = "com.android.settings/.Settings"
    _tenant, platform, farm, service, _store, rental = _created(tmp_path, farm=farm)
    opened = service.control_session(CUSTOMER_A, rental, {"action": "settings"})
    assert opened.http_status == 200
    assert opened.body["restricted_destination"] == "add_esim"
    assert any(c[0] == "home" for c in platform.calls)
    recoveries = _recover_inspects(farm)
    assert len(recoveries) == 1
    farm.inspect_activity = "com.android.settings/com.android.settings.Settings"
    platform.calls.clear()
    farm.tasks.clear()
    tapped = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 80, "y": 80})
    assert tapped.http_status == 200
    assert tapped.body["restricted_destination"] == "add_esim"
    assert any(c[0] == "tap" for c in platform.calls)
    assert any(c[0] == "home" for c in platform.calls)
    assert not any(t["type"] == "provision_esim" for t in farm.tasks)
    stream = service.open_stream(CUSTOMER_A, rental)
    assert getattr(stream, "device_id") == SLOT2_SERIAL


def test_nested_esim_back_and_system_nav_stay_open(tmp_path: Path):
    farm = FakeFarm()
    farm.inspect_activity = "com.google.android.euicc/.ui.ConfirmDownloadActivity"
    _tenant, platform, farm, service, _store, rental = _created(tmp_path, farm=farm)
    nested = service.control_session(CUSTOMER_A, rental, {"action": "back"})
    assert nested.http_status == 200
    assert "restricted_destination" not in nested.body
    assert any(c[0] == "back" for c in platform.calls)
    assert not any(c[0] == "home" for c in platform.calls)
    farm.inspect_activity = "com.android.settings/.network.telephony.MobileNetworkActivity"
    farm.inspect_allowed = True
    platform.calls.clear()
    root_back = service.control_session(CUSTOMER_A, rental, {"action": "back"})
    assert root_back.body["restricted_destination"] == "home"
    assert any(c[0] == "home" for c in platform.calls)
    farm.inspect_activity = "com.google.android.apps.nexuslauncher/.NexusLauncherActivity"
    platform.calls.clear()
    for action, body in (
        ("home", {"action": "home"}),
        ("recents", {"action": "recents"}),
        ("rotate", {"action": "rotate", "orientation": "portrait"}),
    ):
        result = service.control_session(CUSTOMER_A, rental, body)
        assert result.http_status == 200, action
        assert "restricted_destination" not in result.body
    shade = service.control_session(CUSTOMER_A, rental, {"action": "notification_shade"})
    assert shade.body["restriction"] == "shade_blocked"
    assert {c[0] for c in platform.calls} >= {
        "home",
        "recents",
        "rotate",
    }
    assert "notification_shade" not in {c[0] for c in platform.calls}


def test_unknown_settings_activity_fails_open(tmp_path: Path):
    farm = FakeFarm()
    farm.inspect_unknown = True
    _tenant, platform, farm, service, _store, rental = _created(tmp_path, farm=farm)
    tap = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 9, "y": 9})
    assert tap.http_status == 200
    assert "restricted_destination" not in tap.body
    assert any(c[0] == "tap" for c in platform.calls)
    assert not any(c[0] == "home" for c in platform.calls)


def test_restrictions_can_be_toggled_without_touching_gads(tmp_path: Path):
    farm = FakeFarm()
    farm.inspect_activity = "com.android.settings/.Settings"
    _tenant, platform, farm, service, _store, rental = _created(tmp_path, farm=farm)
    on = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 1, "y": 1})
    assert on.body["restricted_destination"] == "add_esim"
    service._session_restrictions = False
    farm.tasks.clear()
    platform.calls.clear()
    off = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 2, "y": 2})
    assert off.http_status == 200
    assert "restricted_destination" not in off.body
    assert _recover_inspects(farm) == []
    service._session_restrictions = True
    farm.inspect_activity = "com.android.settings/.Settings"
    restored = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 3, "y": 3})
    assert restored.body["restricted_destination"] == "add_esim"
    assert not any(c[0] in {"revoke", "release"} for c in platform.calls)


def test_camera_from_launcher_sent_home_like_voidfix(tmp_path: Path):
    farm = FakeFarm()
    farm.inspect_activity = "com.google.android.apps.nexuslauncher/.NexusLauncherActivity"
    _tenant, platform, farm, service, _store, rental = _created(tmp_path, farm=farm)
    launcher = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 10, "y": 10})
    assert launcher.http_status == 200
    assert "restricted_destination" not in launcher.body
    farm.inspect_activity = "com.google.android.GoogleCamera/.CameraActivity"
    platform.calls.clear()
    camera = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 20, "y": 20})
    assert camera.http_status == 200
    assert camera.body["restricted_destination"] == "home"
    assert camera.body["restriction"] == "sensitive_app_blocked"
    assert len([c for c in platform.calls if c[0] == "home"]) == 1
    assert not any(c[0] in {"revoke", "release"} for c in platform.calls)
    assert not any(
        t["type"] == "setup_session_inspect" and t["payload"].get("recover") for t in farm.tasks
    )
    stream = service.open_stream(CUSTOMER_A, rental)
    assert getattr(stream, "device_id") == SLOT2_SERIAL


def test_native_camera_blocked_but_gallery_and_lpa_scan_remain_allowed(tmp_path: Path):
    farm = FakeFarm()
    farm.inspect_activity = "com.android.settings/.network.telephony.MobileNetworkActivity"
    _tenant, platform, farm, service, _store, rental = _created(tmp_path, farm=farm)
    esim = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 12, "y": 12})
    assert esim.http_status == 200
    assert "restricted_destination" not in esim.body
    farm.inspect_activity = "com.google.android.GoogleCamera/.CameraActivity"
    platform.calls.clear()
    camera = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 13, "y": 13})
    assert camera.http_status == 200
    assert camera.body["restricted_destination"] == "home"
    assert camera.body["restriction"] == "sensitive_app_blocked"
    farm.inspect_activity = "com.google.android.photospicker/.PhotoPickerActivity"
    platform.calls.clear()
    gallery = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 14, "y": 14})
    assert gallery.http_status == 200
    assert "restricted_destination" not in gallery.body
    assert not any(c[0] == "home" for c in platform.calls)
    farm.inspect_activity = "com.google.android.euicc/.ui.QrCodeScanActivity"
    platform.calls.clear()
    qr = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 15, "y": 15})
    assert qr.http_status == 200
    assert "restricted_destination" not in qr.body
    assert not any(c[0] == "home" for c in platform.calls)
    stream = service.open_stream(CUSTOMER_A, rental)
    assert getattr(stream, "device_id") == SLOT2_SERIAL


def test_camera_home_is_one_shot_not_a_loop(tmp_path: Path):
    farm = FakeFarm()
    farm.inspect_activity = "com.google.android.GoogleCamera/.CameraActivity"
    _tenant, platform, farm, service, _store, rental = _created(tmp_path, farm=farm)
    first = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 8, "y": 8})
    assert first.body["restricted_destination"] == "home"
    assert len([c for c in platform.calls if c[0] == "home"]) == 1
    farm.inspect_activity = "com.google.android.apps.nexuslauncher/.NexusLauncherActivity"
    platform.calls.clear()
    after = service.control_session(CUSTOMER_A, rental, {"action": "tap", "x": 9, "y": 9})
    assert after.http_status == 200
    assert "restricted_destination" not in after.body
    assert not any(c[0] == "home" for c in platform.calls)
    assert _recover_inspects(farm) == []
    assert not any(c[0] in {"revoke", "release"} for c in platform.calls)
