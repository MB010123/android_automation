"""About-phone screenshot must open the About page, not Settings home."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.device_about_phone import (
    PIXEL_DEVICE_INFO_INTENT,
    UI_DUMP_PATH,
    find_about_phone_tap,
    is_pixel_device,
    run_device_about_phone_screenshot,
    ui_is_about_phone,
    ui_is_settings_home,
)
from application.farm_task_types import FarmTaskRequest
from infrastructure.adb_companion import AdbCommandError, AdbCommandResult, AdbCommandRunner

S20_SERIAL = "RFCNB056WEV"
PIXEL_SERIAL = "18171FDF6005WG"
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00"
    b"\x00\x04\x00\x01\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82"
)

SETTINGS_HOME_XML = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node bounds="[0,0][1080,2400]">
    <node text="Settings" bounds="[0,80][200,140]"/>
    <node text="Connections" bounds="[40,400][400,460]"/>
    <node text="Connected devices" bounds="[40,500][500,560]"/>
    <node text="Sounds and vibration" bounds="[40,600][500,660]"/>
    <node text="About phone" bounds="[40,2000][400,2080]"/>
  </node>
</hierarchy>
"""

ABOUT_PAGE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node bounds="[0,0][1080,2400]">
    <node text="Navigate up" bounds="[20,80][80,140]"/>
    <node text="About phone" bounds="[100,80][400,140]"/>
    <node text="Model name" bounds="[40,400][400,460]"/>
    <node text="SM-G981U1" bounds="[40,460][400,520]"/>
    <node text="Serial number" bounds="[40,540][400,600]"/>
    <node text="RFCNB056WEV" bounds="[40,600][400,660]"/>
    <node text="IMEI" bounds="[40,700][200,760]"/>
    <node text="Status information" bounds="[40,800][500,860]"/>
  </node>
</hierarchy>
"""

PIXEL_ABOUT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node bounds="[0,0][1080,2400]">
    <node text="About phone" bounds="[40,80][400,140]"/>
    <node text="Model" bounds="[40,400][200,460]"/>
    <node text="Pixel 6" bounds="[40,460][400,520]"/>
    <node text="Serial number" bounds="[40,540][400,600]"/>
    <node text="IMEI" bounds="[40,700][200,760]"/>
  </node>
</hierarchy>
"""


class AboutRunner(AdbCommandRunner):
    def __init__(self, *, manufacturer: str = "samsung", page: str = "home") -> None:
        super().__init__()
        self._manufacturer = manufacturer
        self.page = page
        self.calls: list[tuple[str, list[str]]] = []

    def run(self, serial: str, arguments: list[str], **_kwargs) -> AdbCommandResult:
        self.calls.append((serial, list(arguments)))
        joined = " ".join(arguments)
        if arguments == ["get-state"]:
            return AdbCommandResult(stdout="device", stderr="")
        if arguments[:3] == ["shell", "getprop", "ro.product.manufacturer"]:
            return AdbCommandResult(stdout=self._manufacturer, stderr="")
        if arguments[:3] == ["shell", "getprop", "ro.product.model"]:
            model = "SM-G981U1" if self._manufacturer.lower() == "samsung" else "Pixel 6"
            return AdbCommandResult(stdout=model, stderr="")
        if arguments[:3] == ["shell", "uiautomator", "dump"]:
            return AdbCommandResult(stdout="UI hierchary dumped to: " + UI_DUMP_PATH, stderr="")
        if arguments[:2] == ["shell", "cat"] and UI_DUMP_PATH in joined:
            return AdbCommandResult(stdout=self._xml(), stderr="")
        if arguments[:3] == ["shell", "input", "tap"]:
            self.page = "about"
            return AdbCommandResult(stdout="", stderr="")
        if arguments[:3] == ["shell", "am", "start"] and "DEVICE_INFO_SETTINGS" in joined:
            if self._manufacturer.lower() != "samsung":
                self.page = "about"
            return AdbCommandResult(stdout="Starting: Intent", stderr="")
        if arguments[:3] == ["shell", "am", "start"]:
            self.page = "home"
            return AdbCommandResult(stdout="Starting: Intent", stderr="")
        return AdbCommandResult(stdout="", stderr="")

    def run_bytes(self, serial: str, arguments: list[str], **_kwargs) -> bytes:
        self.calls.append((serial, list(arguments)))
        return PNG

    def _xml(self) -> str:
        if self.page == "about":
            return PIXEL_ABOUT_XML if self._manufacturer.lower() != "samsung" else ABOUT_PAGE_XML
        return SETTINGS_HOME_XML


def _request(slot: int = 21) -> FarmTaskRequest:
    return FarmTaskRequest(
        job_id="about-job",
        task_type="device_about_phone_screenshot",
        farm_slot_id=slot,
        payload={},
    )


def test_ui_classifiers_distinguish_settings_home_from_about_phone():
    assert ui_is_settings_home(SETTINGS_HOME_XML) is True
    assert ui_is_about_phone(SETTINGS_HOME_XML) is False
    assert ui_is_about_phone(ABOUT_PAGE_XML) is True
    assert ui_is_settings_home(ABOUT_PAGE_XML) is False
    assert find_about_phone_tap(SETTINGS_HOME_XML) == (220, 2040)


def test_samsung_opens_settings_taps_about_phone_and_uses_bay_serial():
    runner = AboutRunner(manufacturer="samsung")
    result = run_device_about_phone_screenshot(
        adb_path="adb",
        slot_map={21: S20_SERIAL, 1: PIXEL_SERIAL},
        request=_request(21),
        command_runner=runner,
        sleep_fn=lambda _s: None,
    )
    assert result.ok is True
    assert result.http_status == 200
    assert result.details["serial"] == S20_SERIAL
    assert result.details["slot_id"] == 21
    assert result.details["about_page_verified"] is True
    assert result.details["image_base64"].startswith("data:image/png;base64,")
    serials = {serial for serial, _ in runner.calls}
    assert serials == {S20_SERIAL}
    joined = " ".join(" ".join(args) for _s, args in runner.calls)
    assert "com.android.settings/.Settings" in joined
    assert "Settings$MyDeviceInfoActivity" not in joined
    assert "uiautomator dump" in joined
    assert "input tap 220 2040" in joined
    assert any(args[:2] == ["exec-out", "screencap"] for _s, args in runner.calls)


def test_refuses_screenshot_when_settings_home_stays_on_screen():
    class StuckHome(AboutRunner):
        def run(self, serial: str, arguments: list[str]) -> AdbCommandResult:
            result = super().run(serial, arguments)
            self.page = "home"
            return result

    runner = StuckHome(manufacturer="samsung")
    result = run_device_about_phone_screenshot(
        adb_path="adb",
        slot_map={21: S20_SERIAL},
        request=_request(21),
        command_runner=runner,
        sleep_fn=lambda _s: None,
    )
    assert result.ok is False
    assert result.http_status == 422
    assert result.error == "about_phone_not_open"
    assert result.details["about_page_verified"] is False
    assert result.details["settings_home"] is True
    assert "image_base64" not in (result.details or {})
    assert not any(args[:2] == ["exec-out", "screencap"] for _s, args in runner.calls)


def test_pixel_uses_device_info_intent():
    assert is_pixel_device(manufacturer="google", model="Pixel 6") is True
    runner = AboutRunner(manufacturer="google", page="home")
    result = run_device_about_phone_screenshot(
        adb_path="adb",
        slot_map={1: PIXEL_SERIAL, 21: S20_SERIAL},
        request=_request(1),
        command_runner=runner,
        sleep_fn=lambda _s: None,
    )
    assert result.ok is True
    assert result.details["serial"] == PIXEL_SERIAL
    joined = " ".join(" ".join(args) for _s, args in runner.calls)
    assert PIXEL_DEVICE_INFO_INTENT in joined
    assert "--activity-clear-task" in joined
    assert "android.settings.SETTINGS" not in joined
    assert "uiautomator dump" not in joined
    assert "input tap" not in joined
    assert {serial for serial, _ in runner.calls} == {PIXEL_SERIAL}
    kinds = [" ".join(args) for _s, args in runner.calls]
    first_screencap = next(i for i, item in enumerate(kinds) if item.startswith("exec-out screencap"))
    assert any(item.startswith("shell getprop") for item in kinds[:first_screencap])


def test_pixel_screencap_timeout_returns_identity_without_hanging():
    class SlowCap(AboutRunner):
        def run_bytes(self, serial: str, arguments: list[str], **_kwargs) -> bytes:
            self.calls.append((serial, list(arguments)))
            raise AdbCommandError("ADB command timed out")

    runner = SlowCap(manufacturer="google", page="home")
    result = run_device_about_phone_screenshot(
        adb_path="adb",
        slot_map={1: PIXEL_SERIAL},
        request=_request(1),
        command_runner=runner,
        sleep_fn=lambda _s: None,
    )
    assert result.ok is True
    assert result.http_status == 200
    assert result.details["serial"] == PIXEL_SERIAL
    assert result.details["about_page_verified"] is True
    assert "image_base64" not in (result.details or {})
    assert any(str(item).startswith("screencap:") for item in result.details["warnings"])
    joined = " ".join(" ".join(args) for _s, args in runner.calls)
    assert "uiautomator dump" not in joined
    assert "dumpsys euicc" not in joined


def test_slot_4_uses_mapped_serial_only():
    runner = AboutRunner(manufacturer="google", page="home")
    result = run_device_about_phone_screenshot(
        adb_path="adb",
        slot_map={4: PIXEL_SERIAL, 21: S20_SERIAL},
        request=_request(4),
        command_runner=runner,
        sleep_fn=lambda _s: None,
    )
    assert result.ok is True
    assert result.details["slot_id"] == 4
    assert result.details["serial"] == PIXEL_SERIAL
    assert {serial for serial, _ in runner.calls} == {PIXEL_SERIAL}


def test_unmapped_slot_does_not_touch_another_phone():
    runner = AboutRunner()
    result = run_device_about_phone_screenshot(
        adb_path="adb",
        slot_map={1: PIXEL_SERIAL},
        request=_request(21),
        command_runner=runner,
        sleep_fn=lambda _s: None,
    )
    assert result.ok is False
    assert result.http_status == 404
    assert runner.calls == []
