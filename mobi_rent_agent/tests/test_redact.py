from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infrastructure.farm_audit import append_farm_audit
from infrastructure.redact import normalize_msisdn, redact_phone, redact_secret, redact_serial


def test_redact_serial_and_phone():
    assert redact_serial("1C101FDF6009EZ") == "1C10****09EZ"
    assert redact_phone("+15551234567") == "+XXX****4567"
    assert redact_secret("key=abc", "abc") == "key=<redacted>"
    assert normalize_msisdn("+1 (555) 123-4567") == "15551234567"


def test_audit_drops_secret_keys(tmp_path):
    path = tmp_path / "audit.jsonl"
    append_farm_audit(
        {
            "event": "test",
            "api_key": "should-not-appear",
            "slot_id": 1,
            "overall": "PASS",
        },
        path=path,
    )
    text = path.read_text(encoding="utf-8")
    assert "should-not-appear" not in text
    assert "api_key" not in text
    assert "PASS" in text
