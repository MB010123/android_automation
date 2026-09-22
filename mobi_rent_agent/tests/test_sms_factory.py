from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from application.sms_factory import (
    DEFAULT_VOIDFIX_SEND_SIM_SLOT,
    build_sms_dispatch_service,
    sms_dispatch_runtime_flags,
)
from application.sms_outbox import SmsOutbox
from domain.farm import PROTOTYPE_VOIDFIX_DEVICE_ID
from infrastructure.config import AgentConfig


@pytest.fixture(autouse=True)
def _memory_sms_outbox(monkeypatch):
    monkeypatch.setattr(
        "application.sms_factory.create_durable_sms_outbox",
        lambda path=None: SmsOutbox(),
    )


def _base_config(**overrides) -> AgentConfig:
    data = dict(
        supabase_url="https://example.supabase.co",
        supabase_anon_key="k",
        supabase_table="hardware_queue",
        hardware_agent_token="t",
        heartbeat_interval_seconds=15.0,
        request_timeout_seconds=10.0,
        log_level="INFO",
        adb_path="adb",
        voidfix_enabled=True,
        voidfix_api_key="secret",
        voidfix_allowed_slot_ids=(1,),
        voidfix_device_map_path="voidfix_devices.json",
        voidfix_delivery_poll_enabled=True,
        voidfix_sim_slot_send_enabled=True,
        voidfix_live_send_authorized=True,
        voidfix_dry_run=False,
        voidfix_recipient_allowlist=("+15551234567",),
    )
    data.update(overrides)
    return AgentConfig(**data)


def test_factory_wires_delivery_poller_and_sim_slot(tmp_path: Path, monkeypatch):
    device_map = tmp_path / "voidfix_devices.json"
    device_map.write_text('{"1": "1386"}', encoding="utf-8")
    config = _base_config(voidfix_device_map_path=str(device_map))

    service = build_sms_dispatch_service(config)
    assert service is not None
    assert service._delivery_poller is not None
    assert service._voidfix_sim_slots == {1: DEFAULT_VOIDFIX_SEND_SIM_SLOT}


def test_factory_excludes_prototype_device_id(tmp_path: Path):
    device_map = tmp_path / "voidfix_devices.json"
    device_map.write_text(
        f'{{"1": "1386", "2": "{PROTOTYPE_VOIDFIX_DEVICE_ID}"}}',
        encoding="utf-8",
    )
    config = _base_config(
        voidfix_device_map_path=str(device_map),
        voidfix_allowed_slot_ids=(1, 2),
    )
    # slot 2 is allowlisted but maps only to prototype 1385, which is excluded
    assert build_sms_dispatch_service(config) is None


def test_factory_requires_allowed_slot_in_device_map(tmp_path: Path):
    device_map = tmp_path / "voidfix_devices.json"
    device_map.write_text('{"1": "1386"}', encoding="utf-8")
    config = _base_config(
        voidfix_device_map_path=str(device_map),
        voidfix_allowed_slot_ids=(1, 2),
    )
    assert build_sms_dispatch_service(config) is None


def test_factory_no_poller_when_delivery_poll_disabled(tmp_path: Path):
    device_map = tmp_path / "voidfix_devices.json"
    device_map.write_text('{"1": "1386"}', encoding="utf-8")
    config = _base_config(
        voidfix_device_map_path=str(device_map),
        voidfix_delivery_poll_enabled=False,
    )
    service = build_sms_dispatch_service(config)
    assert service is not None
    assert service._delivery_poller is None


def test_runtime_flags_reflect_config(tmp_path: Path, monkeypatch):
    device_map = tmp_path / "voidfix_devices.json"
    device_map.write_text('{"1": "1386"}', encoding="utf-8")
    config = _base_config(voidfix_device_map_path=str(device_map))
    flags = sms_dispatch_runtime_flags(config)
    assert flags["delivery_poll_enabled"] is True
    assert flags["sim_slot_send_enabled"] is True
    assert flags["delivery_poller_active"] is True
    assert flags["dispatch_service_constructed"] is True
