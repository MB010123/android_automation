from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infrastructure.adb_imei import AdbImeiReader
from infrastructure.imei import is_valid_imei, parse_cmd_phone_imei, redact_imei


def test_valid_imei_luhn():
    # Public example IMEI used in GSM documentation (not a farm device).
    assert is_valid_imei("490154203237518") is True
    assert is_valid_imei("490154203237519") is False
    assert is_valid_imei("123") is False


def test_redact_hides_middle_digits():
    redacted = redact_imei("490154203237518")
    assert redacted == "4901…7518"
    assert "490154203237518" not in redacted


def test_parse_permission_denied():
    with pytest.raises(PermissionError):
        parse_cmd_phone_imei("Device IMEI: Permission denied.")


def test_parse_success():
    assert parse_cmd_phone_imei("Device IMEI: 490154203237518") == "490154203237518"


class FakeRunner:
    def __init__(self) -> None:
        self._adb_path = "adb"


def test_adb_reader_records_denial_without_values(monkeypatch):
    class Completed:
        stdout = "Device IMEI: Permission denied."
        stderr = ""

    monkeypatch.setattr(
        "infrastructure.adb_imei.subprocess.run",
        lambda *_args, **_kwargs: Completed(),
    )
    result = AdbImeiReader(FakeRunner()).read("SERIAL-1")
    assert result.imei1_accessible is False
    assert result.imei2_accessible is False
    assert result.imei1 is None
    assert result.imei2 is None
    assert "Permission denied" in (result.error or "")
