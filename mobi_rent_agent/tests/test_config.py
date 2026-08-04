"""Unit tests for infrastructure.config.load_config.

No .env file or real environment is touched: env_file=None skips dotenv
loading and monkeypatch controls exactly the variables each test needs.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infrastructure.config import ConfigError, load_config

REQUIRED_ENV = {
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_ANON_KEY": "anon-key-123",
    "HARDWARE_AGENT_TOKEN": "hw-token-123",
}


def _set_required(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize("missing_key", list(REQUIRED_ENV))
def test_missing_required_variable_raises_config_error(monkeypatch, missing_key):
    _set_required(monkeypatch)
    monkeypatch.delenv(missing_key, raising=False)

    with pytest.raises(ConfigError):
        load_config(env_file=None)


def test_minimal_config_applies_documented_defaults(monkeypatch):
    _set_required(monkeypatch)
    for optional in (
        "SUPABASE_TABLE",
        "HEARTBEAT_INTERVAL_SECONDS",
        "REQUEST_TIMEOUT_SECONDS",
        "HEARTBEAT_ENDPOINT",
        "HEALTH_MONITOR_ENABLED",
    ):
        monkeypatch.delenv(optional, raising=False)

    config = load_config(env_file=None)

    assert config.supabase_table == "hardware_queue"
    assert config.heartbeat_interval_seconds == 15.0
    assert config.request_timeout_seconds == 10.0
    assert config.health_monitor_enabled is False
    assert config.heartbeat_endpoint is None
    assert config.queue_endpoint == "https://example.supabase.co/rest/v1/hardware_queue"


def test_dedicated_heartbeat_endpoint_overrides_supabase_rest_url(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("HEARTBEAT_ENDPOINT", "https://mobi-rent.example/api/public/hardware/queue")

    config = load_config(env_file=None)

    assert config.queue_endpoint == "https://mobi-rent.example/api/public/hardware/queue"


def test_invalid_numeric_value_raises_config_error(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("HEARTBEAT_INTERVAL_SECONDS", "not-a-number")

    with pytest.raises(ConfigError):
        load_config(env_file=None)


@pytest.mark.parametrize("raw_value,expected", [
    ("true", True),
    ("TRUE", True),
    ("1", True),
    ("yes", True),
    ("on", True),
    ("false", False),
    ("0", False),
    ("no", False),
    ("off", False),
])
def test_health_monitor_enabled_accepts_documented_boolean_spellings(monkeypatch, raw_value, expected):
    _set_required(monkeypatch)
    monkeypatch.setenv("HEALTH_MONITOR_ENABLED", raw_value)

    config = load_config(env_file=None)

    assert config.health_monitor_enabled is expected


def test_health_monitor_enabled_rejects_unrecognized_value(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("HEALTH_MONITOR_ENABLED", "maybe")

    with pytest.raises(ConfigError):
        load_config(env_file=None)
