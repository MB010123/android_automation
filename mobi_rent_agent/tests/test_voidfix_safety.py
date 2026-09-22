from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infrastructure.config import AgentConfig
from main import _log_voidfix_state


def _config(**overrides) -> AgentConfig:
    values = dict(
        supabase_url="https://example.supabase.co",
        supabase_anon_key="anon",
        supabase_table="hardware_queue",
        hardware_agent_token="tok",
        heartbeat_interval_seconds=15.0,
        request_timeout_seconds=10.0,
        log_level="INFO",
        adb_path="adb",
        voidfix_enabled=False,
        voidfix_api_key=None,
    )
    values.update(overrides)
    return AgentConfig(**values)


def test_disabled_voidfix_does_not_construct_gateway(monkeypatch, caplog):
    def boom(*_args, **_kwargs):
        raise AssertionError("VoidFixSmsGateway must not be constructed when disabled")

    monkeypatch.setattr("main.VoidFixSmsGateway", boom)
    with caplog.at_level("INFO"):
        _log_voidfix_state(_config(voidfix_enabled=False, voidfix_api_key=None))
    assert "disabled by configuration" in caplog.text


def test_key_present_but_disabled_does_not_construct_gateway(monkeypatch, caplog):
    def boom(*_args, **_kwargs):
        raise AssertionError("VoidFixSmsGateway must not be constructed when VOIDFIX_ENABLED is false")

    monkeypatch.setattr("main.VoidFixSmsGateway", boom)
    with caplog.at_level("INFO"):
        _log_voidfix_state(_config(voidfix_enabled=False, voidfix_api_key="present-but-disabled"))
    assert "VOIDFIX_ENABLED is false" in caplog.text
    assert "present-but-disabled" not in caplog.text


def test_enabled_without_key_does_not_construct_gateway(monkeypatch, caplog):
    def boom(*_args, **_kwargs):
        raise AssertionError("VoidFixSmsGateway must not be constructed without an API key")

    monkeypatch.setattr("main.VoidFixSmsGateway", boom)
    with caplog.at_level("INFO"):
        _log_voidfix_state(_config(voidfix_enabled=True, voidfix_api_key=None))
    assert "VOIDFIX_API_KEY is unset" in caplog.text
