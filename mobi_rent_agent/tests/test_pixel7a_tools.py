from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT.parent / "pixel7a_sandbox"
SANDBOX_TOOLS = (
    ARCHIVE / "pixel7a_live_download.py",
    ARCHIVE / "sandbox_switch_esim.py",
    ARCHIVE / "pixel7a_lifecycle_validate.py",
    ROOT / "infrastructure" / "sandbox_device.py",
    ROOT / "infrastructure" / "tello_profile_state.py",
)
FORBIDDEN = (
    "load_slot_map",
    "range(2, 21)",
    "range(2,21)",
    "1C101FDF6009EZ",
)


def test_sandbox_tools_do_not_import_farm_slot_map():
    for path in SANDBOX_TOOLS:
        source = path.read_text(encoding="utf-8")
        for token in FORBIDDEN:
            assert token not in source, f"{path.name} contains {token}"


def test_pixel7a_archive_hardcodes_sandbox_serial():
    source = (ARCHIVE / "pixel7a_live_download.py").read_text(encoding="utf-8")
    assert "3C071JEHN14705" in source
    assert "switch_after_download_reached_enabled" in source
    assert "PIXEL7A_ACTIVATION_CODE" in source
    assert "collect_sandbox_profile" in source


def test_lifecycle_script_is_carrier_agnostic():
    source = (ARCHIVE / "pixel7a_lifecycle_validate.py").read_text(encoding="utf-8")
    assert "US Mobile" not in source
    assert "Verizon" not in source
    assert "Tello" not in source
    assert "collect_sandbox_profile" in source
    assert "3C071JEHN14705" in source


def test_profile_parser_source_has_no_carrier_allowlist():
    source = (ROOT / "infrastructure" / "tello_profile_state.py").read_text(encoding="utf-8")
    assert "SANDBOX_BRANDS" not in source
    assert "Verizon" not in source
    assert "US Mobile" not in source
