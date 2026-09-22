"""Tests for read-only production validation."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infrastructure.config import ConfigError, load_config
from infrastructure.production_validate import validate_production_config


def test_validate_minimal_env_only(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    monkeypatch.setenv("HARDWARE_AGENT_TOKEN", "token")
    for key in ("VOIDFIX_ENABLED", "VOIDFIX_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    report = validate_production_config(env_file=None, project_root=tmp_path, expect_farm_slots=2)
    assert report.app_name == "mobi-rent-agent"
    assert any(i.code == "slot_map_missing" for i in report.issues)


def test_validate_slot_map_complete(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    monkeypatch.setenv("HARDWARE_AGENT_TOKEN", "token")
    slot_map = tmp_path / "slot_map.json"
    slot_map.write_text(json.dumps({"1": "serial-a", "2": "serial-b"}), encoding="utf-8")
    monkeypatch.setenv("SLOT_MAP_PATH", str(slot_map))

    report = validate_production_config(env_file=None, project_root=tmp_path, expect_farm_slots=2)
    assert report.ok is True
    assert not any(i.level == "error" for i in report.issues)


def test_log_file_and_outbox_path_defaults(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    monkeypatch.setenv("HARDWARE_AGENT_TOKEN", "token")
    monkeypatch.setenv("LOG_FILE", "/var/log/mobi-rent/agent.log")
    monkeypatch.setenv("SMS_OUTBOX_DB_PATH", "/var/lib/mobi-rent/sms_outbox.sqlite")
    monkeypatch.setenv("APP_NAME", "farm-agent")

    config = load_config(env_file=None)
    assert config.log_file == "/var/log/mobi-rent/agent.log"
    assert config.sms_outbox_db_path == "/var/lib/mobi-rent/sms_outbox.sqlite"
    assert config.app_name == "farm-agent"
