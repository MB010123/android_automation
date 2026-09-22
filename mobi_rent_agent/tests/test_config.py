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
        "PROVISIONING_ENDPOINT",
        "PROVISIONING_ALLOWED_SLOT_IDS",
        "REAL_ESIM_ENABLED",
        "ESIM_LIVE_DOWNLOAD_ARMED",
        "VOIDFIX_ENABLED",
        "VOIDFIX_API_KEY",
        "VOIDFIX_ALLOWED_SLOT_IDS",
        "HEALTH_RECOVERY_ENABLED",
        "VOIDFIX_LIVE_SEND_AUTHORIZED",
        "VOIDFIX_DRY_RUN",
        "VOIDFIX_RECIPIENT_ALLOWLIST",
        "VOIDFIX_SIM_SLOT_SEND_ENABLED",
        "VOIDFIX_DELIVERY_POLL_ENABLED",
        "VOIDFIX_DELIVERY_POLL_INTERVAL_SECONDS",
        "VOIDFIX_DELIVERY_POLL_TIMEOUT_SECONDS",
        "FARM_MAX_CONCURRENT",
        "SMS_MAX_ATTEMPTS",
    ):
        monkeypatch.delenv(optional, raising=False)

    config = load_config(env_file=None)

    assert config.supabase_table == "hardware_queue"
    assert config.heartbeat_interval_seconds == 15.0
    assert config.request_timeout_seconds == 10.0
    assert config.health_monitor_enabled is False
    assert config.heartbeat_endpoint is None
    assert config.queue_endpoint == "https://example.supabase.co/rest/v1/hardware_queue"
    assert config.provisioning_endpoint is None
    assert config.provisioning_allowed_slot_ids == (1,)
    assert config.real_esim_enabled is False
    assert config.esim_live_download_armed is False
    assert config.voidfix_enabled is False
    assert config.voidfix_api_key is None
    assert config.sms_enabled is False
    assert config.voidfix_allowed_slot_ids == ()
    assert config.voidfix_send_endpoint == "https://sms.voidfix.com/services/send.php"
    assert config.health_recovery_enabled is False
    assert config.voidfix_live_send_authorized is False
    assert config.voidfix_dry_run is True
    assert config.voidfix_recipient_allowlist == ()
    assert config.voidfix_sim_slot_send_enabled is False
    assert config.voidfix_delivery_poll_enabled is False
    assert config.voidfix_delivery_poll_interval_seconds == 5.0
    assert config.voidfix_delivery_poll_timeout_seconds == 180.0
    assert config.farm_max_concurrent == 4
    assert config.sms_max_attempts == 1


def test_voidfix_delivery_and_sim_slot_flags_load_true(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("VOIDFIX_DELIVERY_POLL_ENABLED", "true")
    monkeypatch.setenv("VOIDFIX_SIM_SLOT_SEND_ENABLED", "true")
    config = load_config(env_file=None)
    assert config.voidfix_delivery_poll_enabled is True
    assert config.voidfix_sim_slot_send_enabled is True


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


def test_provisioning_allowed_slot_ids_parses_csv(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("PROVISIONING_ALLOWED_SLOT_IDS", "1,2")

    config = load_config(env_file=None)

    assert config.provisioning_allowed_slot_ids == (1, 2)


def test_empty_provisioning_allowlist_stays_empty(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("PROVISIONING_ALLOWED_SLOT_IDS", "")

    config = load_config(env_file=None)

    assert config.provisioning_allowed_slot_ids == ()


def test_real_esim_enabled_defaults_false_and_is_not_authorization(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("REAL_ESIM_ENABLED", "true")

    config = load_config(env_file=None)

    assert config.real_esim_enabled is True
    from domain.esim_capabilities import AndroidAuthorizationSnapshot, derive_esim_capabilities

    caps = derive_esim_capabilities(AndroidAuthorizationSnapshot(real_esim_flag=config.real_esim_enabled))
    assert caps.unattended is False
    assert caps.can_download is False
    assert caps.authorization_source == "none"
    assert "not authorization" in caps.reason


def test_live_download_armed_requires_explicit_flag(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("REAL_ESIM_ENABLED", "true")
    monkeypatch.delenv("ESIM_LIVE_DOWNLOAD_ARMED", raising=False)

    config = load_config(env_file=None)

    assert config.real_esim_enabled is True
    assert config.esim_live_download_armed is False


def test_voidfix_requires_explicit_enable_even_when_key_is_set(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("VOIDFIX_API_KEY", "present-but-not-enough")
    monkeypatch.delenv("VOIDFIX_ENABLED", raising=False)

    config = load_config(env_file=None)

    assert config.voidfix_api_key == "present-but-not-enough"
    assert config.voidfix_enabled is False
    assert config.sms_enabled is False


def test_voidfix_enabled_with_key_and_allowlist(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("VOIDFIX_ENABLED", "true")
    monkeypatch.setenv("VOIDFIX_API_KEY", "test-key")
    monkeypatch.setenv("VOIDFIX_ALLOWED_SLOT_IDS", "1")
    monkeypatch.setenv("VOIDFIX_DEVICE_MAP_PATH", "voidfix_devices.json")

    config = load_config(env_file=None)

    assert config.sms_enabled is True
    assert config.voidfix_allowed_slot_ids == (1,)
    assert config.voidfix_device_map_path == "voidfix_devices.json"


def test_voidfix_empty_allowlist_stays_empty(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("VOIDFIX_ALLOWED_SLOT_IDS", "")

    config = load_config(env_file=None)

    assert config.voidfix_allowed_slot_ids == ()


def test_voidfix_enabled_without_key_stays_disabled(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("VOIDFIX_ENABLED", "true")
    monkeypatch.delenv("VOIDFIX_API_KEY", raising=False)

    config = load_config(env_file=None)

    assert config.voidfix_enabled is True
    assert config.voidfix_api_key is None
    assert config.sms_enabled is False


def test_voidfix_enabled_with_empty_allowlist_does_not_imply_slots(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("VOIDFIX_ENABLED", "true")
    monkeypatch.setenv("VOIDFIX_API_KEY", "test-key")
    monkeypatch.setenv("VOIDFIX_ALLOWED_SLOT_IDS", "")

    config = load_config(env_file=None)

    assert config.sms_enabled is True
    assert config.voidfix_allowed_slot_ids == ()
