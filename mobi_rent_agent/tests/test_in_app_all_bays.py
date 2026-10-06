"""Customer in-app setup for Farm bays 1–20 (unique GADS workspaces)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infrastructure.config import load_config
from infrastructure.gads_workspaces import unique_workspace_map
from infrastructure.remote_access_store import STATUS_ACTIVE
from tests.test_remote_access_poc import (
    CUSTOMER_A,
    CUSTOMER_B,
    FakeFarm,
    FakePlatform,
    SLOT1_SERIAL,
    SLOT2_SERIAL,
    _rental,
    _service,
)
from tests.fakes_supabase import MemoryTenant

CUSTOMER_BAYS = (1, 2, 8, 15, 20)
SECRET_MARKERS = (
    "SERIAL-",
    "udid",
    "workspace",
    "gads",
    "password",
    "admin-jwt",
    "access_token",
    "one-time-secret",
)


def _all_serials() -> dict[int, str]:
    mapping = {bay: f"SERIAL-BAY{bay:02d}" for bay in range(1, 21)}
    mapping[1] = SLOT1_SERIAL
    mapping[2] = SLOT2_SERIAL
    return mapping


def _all_workspaces() -> dict[int, str]:
    return {bay: f"ws-bay-{bay:02d}" for bay in range(1, 21)}


def _all_bay_service(tmp_path: Path, *, tenant: MemoryTenant, farm: FakeFarm | None = None, **kwargs):
    serials = _all_serials()
    workspaces = _all_workspaces()
    platform = kwargs.pop("platform", None) or FakePlatform(registered=set(serials.values()))
    platform.online.update({serial: True for serial in serials.values()})
    if not platform.device_workspace:
        platform.device_workspace = {serials[bay]: workspaces[bay] for bay in serials}
    prepare_slot_ids = kwargs.pop("prepare_slot_ids", None)
    observe_slot_ids = kwargs.pop("observe_slot_ids", None)
    voidfix_package = kwargs.pop("voidfix_package", "com.voidfix.app")
    return _service(
        tmp_path,
        tenant=tenant,
        platform=platform,
        farm=farm or FakeFarm(),
        allowed=tuple(range(1, 21)),
        gads_slot_ids=None,
        slot_map=serials,
        workspace_map=workspaces,
        prepare_slot_ids=prepare_slot_ids,
        observe_slot_ids=observe_slot_ids,
        voidfix_package=voidfix_package,
        **kwargs,
    )


def _assert_no_secrets(body: dict) -> None:
    blob = json.dumps(body).lower()
    for marker in SECRET_MARKERS:
        assert marker.lower() not in blob, marker
    for serial in _all_serials().values():
        assert serial not in json.dumps(body)
    for workspace in _all_workspaces().values():
        assert workspace not in json.dumps(body)


def test_twenty_unique_workspace_mappings_resolve(tmp_path: Path):
    mapping = unique_workspace_map(_all_workspaces())
    assert len(mapping) == 20
    assert len(set(mapping.values())) == 20
    assert set(mapping) == set(range(1, 21))
    tenant = MemoryTenant()
    service, _, _ = _all_bay_service(tmp_path, tenant=tenant)
    for bay, serial in _all_serials().items():
        assert service.device_for_slot(bay) == serial
        assert service._workspace_for(bay) == _all_workspaces()[bay]


def test_shared_workspace_is_dropped_for_every_sharing_bay():
    cleaned = unique_workspace_map({bay: "shared" for bay in range(1, 21)})
    assert cleaned == {}
    mixed = unique_workspace_map({**{bay: f"ws-{bay}" for bay in range(1, 20)}, 20: "ws-1"})
    assert 1 not in mixed and 20 not in mixed
    assert mixed[2] == "ws-2"
    assert len(mixed) == 18


def test_customer_setup_flow_on_slots_1_2_8_15_20(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    farm.activation_details = {
        "verdict": "ACTIVATION_CONFIRMED",
        "esim_profile_present": True,
        "esim_enabled": True,
        "network_registered": True,
        "cellular": True,
        "observation_complete": True,
    }
    service, store, _ = _all_bay_service(tmp_path, tenant=tenant, farm=farm)
    serials = _all_serials()
    for bay in CUSTOMER_BAYS:
        rental = _rental(tenant, bay=bay, user_id=CUSTOMER_A if bay != 15 else CUSTOMER_B)
        owner = CUSTOMER_A if bay != 15 else CUSTOMER_B
        created = service.create_remote_access(owner, None, rental)
        assert created.http_status == 201, (bay, created.body)
        assert created.body["session_mode"] == "in_app"
        assert "platform_login" not in created.body
        _assert_no_secrets(created.body)
        grants = [c for c in service._platform.calls if c[0] == "grant"]  # type: ignore[union-attr]
        assert grants[-1][1]["device_id"] == serials[bay]
        assert grants[-1][1]["workspace_id"] == _all_workspaces()[bay]
        tap = service.control_session(owner, rental, {"action": "tap", "x": 40, "y": 80})
        assert tap.http_status == 200, (bay, tap.body)
        _assert_no_secrets(tap.body)
        stream = service.open_stream(owner, rental)
        assert getattr(stream, "device_id") == serials[bay]
        observed = service.activation_status_for_customer(owner, rental)
        assert observed.http_status == 200, (bay, observed.body)
        _assert_no_secrets(observed.body)
        store.set_activation_observed(rental, "confirmed")
        done = service.complete_setup(owner, rental)
        assert done.http_status == 200, (bay, done.body)
        assert done.body["ui_state"] == "phone_ready"
        _assert_no_secrets(done.body)
        assert store.get(rental).status != STATUS_ACTIVE
        inspect = [t for t in farm.tasks if t["type"] == "setup_session_inspect" and t["slot"] == bay]
        cycle = [t for t in farm.tasks if t["type"] == "setup_session_voidfix_cycle" and t["slot"] == bay]
        assert inspect and cycle


def test_cross_rental_cannot_access_another_bay(tmp_path: Path):
    tenant = MemoryTenant()
    service, _, _ = _all_bay_service(tmp_path, tenant=tenant)
    rental_2 = _rental(tenant, bay=2, user_id=CUSTOMER_A)
    rental_15 = _rental(tenant, bay=15, user_id=CUSTOMER_B)
    assert service.create_remote_access(CUSTOMER_A, None, rental_2).http_status == 201
    assert service.create_remote_access(CUSTOMER_B, None, rental_15).http_status == 201
    assert service.open_stream(CUSTOMER_A, rental_15).http_status == 403
    assert service.control_session(CUSTOMER_A, rental_15, {"action": "tap", "x": 1, "y": 1}).http_status == 403
    assert service.complete_setup(CUSTOMER_A, rental_15).http_status == 403
    assert service.activation_status_for_customer(CUSTOMER_A, rental_15).http_status == 403
    assert service.create_remote_access(CUSTOMER_A, 15, rental_2).http_status == 403
    assert service.open_stream(CUSTOMER_B, rental_2).http_status == 403
    stream_own = service.open_stream(CUSTOMER_A, rental_2)
    assert getattr(stream_own, "device_id") == SLOT2_SERIAL


def test_missing_workspace_bay_cannot_open_session(tmp_path: Path):
    tenant = MemoryTenant()
    workspaces = _all_workspaces()
    del workspaces[20]
    serials = _all_serials()
    platform = FakePlatform(registered=set(serials.values()))
    platform.online.update({serial: True for serial in serials.values()})
    service, _, _ = _service(
        tmp_path,
        tenant=tenant,
        platform=platform,
        farm=FakeFarm(),
        allowed=tuple(range(1, 21)),
        gads_slot_ids=None,
        slot_map=serials,
        workspace_map=workspaces,
        prepare_slot_ids=None,
        observe_slot_ids=None,
    )
    rental = _rental(tenant, bay=20, user_id=CUSTOMER_A)
    denied = service.create_remote_access(CUSTOMER_A, None, rental)
    assert denied.http_status == 403
    assert service.device_for_slot(20) is None
    assert service.device_for_slot(1) == SLOT1_SERIAL


def test_explicit_prepare_observe_csv_still_restricts(tmp_path: Path):
    tenant = MemoryTenant()
    farm = FakeFarm()
    service, _, _ = _all_bay_service(
        tmp_path,
        tenant=tenant,
        farm=farm,
        prepare_slot_ids=(1,),
        observe_slot_ids=(1,),
    )
    rental = _rental(tenant, bay=8, user_id=CUSTOMER_A)
    assert service.create_remote_access(CUSTOMER_A, None, rental).http_status == 201
    assert service.prepare_esim(CUSTOMER_A, rental).http_status == 403
    assert service.activation_status_for_customer(CUSTOMER_A, rental).http_status == 403
    assert not any(t["type"] == "remote_access_activation_status" for t in farm.tasks)


def test_unset_prepare_observe_env_follows_gads_bays(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("REMOTE_ACCESS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    monkeypatch.setenv("HARDWARE_AGENT_TOKEN", "tok")
    config = load_config(env_file=None)
    assert config.remote_access_prepare_slot_ids is None
    assert config.remote_access_observe_slot_ids is None
    assert config.provisioning_allowed_slot_ids == (1,)
    assert config.remote_access_poc_slot_ids == (1,)
    monkeypatch.setenv("REMOTE_ACCESS_PREPARE_SLOT_IDS", "1")
    monkeypatch.setenv("REMOTE_ACCESS_OBSERVE_SLOT_IDS", "1,2")
    restricted = load_config(env_file=None)
    assert restricted.remote_access_prepare_slot_ids == (1,)
    assert restricted.remote_access_observe_slot_ids == (1, 2)
