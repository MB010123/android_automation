"""Open About Phone on the mapped bay, then capture a live screenshot.

Samsung One UI aliases ``Settings$MyDeviceInfoActivity`` to the Settings
homepage (``com.android.settings/.Settings``), so a direct component launch
plus a fixed sleep captured the wrong screen. This task:

1. Resolves the requested bay through ``slot_map``
2. Wakes that serial
3. Opens Settings
4. Scrolls and taps **About phone** using the live UI tree (not hardcoded taps)
5. Waits until About phone content is on screen
6. Captures ``screencap`` only after that check
"""
from __future__ import annotations

import base64
import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import Any, Callable

from application.device_screen_prepare import (
    is_samsung_s20,
    run_best_effort_shell,
    wake_unlock_commands,
)
from application.samsung_one_ui import is_samsung_device, samsung_settings_args
from application.farm_task_types import FarmTaskRequest, FarmTaskResult
from domain.farm import MAX_FARM_SLOT_ID
from infrastructure.adb_companion import AdbCommandError, AdbCommandRunner
from infrastructure.adb_imei import AdbImeiReader

logger = logging.getLogger("farm_agent.about_phone")

ABOUT_PHONE_TASK = "device_about_phone_screenshot"
ABOUT_PHONE_TIMEOUT_SECONDS = 12.0
ABOUT_PHONE_OVERALL_SECONDS = 28.0
UI_DUMP_PATH = "/sdcard/mobirent_about_phone.xml"
PIXEL_DEVICE_INFO_INTENT = "android.settings.DEVICE_INFO_SETTINGS"
ABOUT_LABELS = ("About phone", "About device", "My device")
_ABOUT_DETAIL_MARKERS = (
    "model name",
    "serial number",
    "imei",
    "status information",
    "software information",
    "battery information",
)
_SETTINGS_HOME_MARKERS = (
    "connections",
    "connected devices",
    "sounds and vibration",
    "network & internet",
    "samsung account",
)
_BOUNDS_RE = re.compile(r"-?\d+")
_EID_RE = re.compile(r"\b(89\d{18,32})\b")
_MINIMAL_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00"
    b"\x00\x04\x00\x01\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82"
)
MAX_SCROLLS = 6


def _serial_for_slot(slot_map: dict[int, str], farm_slot_id: int) -> str | None:
    serial = str(slot_map.get(int(farm_slot_id)) or "").strip()
    return serial or None


def _ui_blob(ui_xml: str) -> str:
    return (ui_xml or "").lower()


def ui_is_settings_home(ui_xml: str) -> bool:
    blob = _ui_blob(ui_xml)
    if not blob:
        return False
    if ui_is_about_phone(ui_xml):
        return False
    return any(marker in blob for marker in _SETTINGS_HOME_MARKERS)


def ui_is_about_phone(ui_xml: str) -> bool:
    """True when the About phone *page* is showing, not the Settings list row."""
    blob = _ui_blob(ui_xml)
    if not blob:
        return False
    details = sum(1 for marker in _ABOUT_DETAIL_MARKERS if marker in blob)
    titled = any(label.lower() in blob for label in ABOUT_LABELS)
    return titled and details >= 2


def _parse_ui(ui_xml: str) -> ET.Element | None:
    text = ui_xml or ""
    start = text.find("<")
    if start < 0:
        return None
    try:
        return ET.fromstring(text[start:])
    except ET.ParseError:
        return None


def _center(bounds: str) -> tuple[int, int] | None:
    nums = [int(v) for v in _BOUNDS_RE.findall(bounds or "")]
    if len(nums) < 4:
        return None
    return (nums[0] + nums[2]) // 2, (nums[1] + nums[3]) // 2


def find_about_phone_tap(ui_xml: str) -> tuple[int, int] | None:
    root = _parse_ui(ui_xml)
    if root is None:
        return None
    needles = tuple(label.lower() for label in ABOUT_LABELS)
    for node in root.iter("node"):
        label = f"{node.get('text') or ''} {node.get('content-desc') or ''}".strip().lower()
        if any(needle == label or needle in label for needle in needles):
            point = _center(node.get("bounds") or "")
            if point:
                return point
    return None


def _visible_size(ui_xml: str) -> tuple[int, int]:
    root = _parse_ui(ui_xml)
    if root is None:
        return (1080, 2400)
    point = _center(root.get("bounds") or "")
    nums = [int(v) for v in _BOUNDS_RE.findall(root.get("bounds") or "")]
    if len(nums) >= 4:
        width = max(1, nums[2] - nums[0])
        height = max(1, nums[3] - nums[1])
        return width, height
    if point:
        return max(point[0] * 2, 1), max(point[1] * 2, 1)
    return (1080, 2400)


def _dump_ui(runner: AdbCommandRunner, serial: str, warnings: list[str]) -> str:
    try:
        runner.run(serial, ["shell", "uiautomator", "dump", UI_DUMP_PATH])
    except AdbCommandError as exc:
        warnings.append(f"ui_dump: {exc.__class__.__name__}")
        return ""
    try:
        return runner.run(serial, ["shell", "cat", UI_DUMP_PATH]).stdout or ""
    except AdbCommandError as exc:
        warnings.append(f"ui_cat: {exc.__class__.__name__}")
        return ""


def _tap(runner: AdbCommandRunner, serial: str, x: int, y: int) -> bool:
    try:
        runner.run(serial, ["shell", "input", "tap", str(x), str(y)])
        return True
    except AdbCommandError:
        return False


def _swipe_settings_list(runner: AdbCommandRunner, serial: str, ui_xml: str) -> None:
    width, height = _visible_size(ui_xml)
    x = max(1, width // 2)
    y1 = max(2, (height * 3) // 4)
    y2 = max(1, height // 4)
    try:
        runner.run(serial, ["shell", "input", "swipe", str(x), str(y1), str(x), str(y2), "400"])
    except AdbCommandError:
        return


def _start_settings(runner: AdbCommandRunner, serial: str, samsung: bool, warnings: list[str]) -> None:
    args = ["shell", *samsung_settings_args()] if samsung else [
        "shell",
        "am",
        "start",
        "-a",
        "android.settings.SETTINGS",
    ]
    try:
        runner.run(serial, args)
    except AdbCommandError as exc:
        warnings.append(f"open_settings: {exc.__class__.__name__}")


def _start_pixel_device_info(runner: AdbCommandRunner, serial: str, warnings: list[str]) -> None:
    try:
        runner.run(serial, ["shell", "am", "start", "-a", PIXEL_DEVICE_INFO_INTENT])
    except AdbCommandError as exc:
        warnings.append(f"device_info_intent: {exc.__class__.__name__}")


def open_about_phone_page(
    runner: AdbCommandRunner,
    serial: str,
    *,
    samsung: bool,
    warnings: list[str],
    sleep_fn: Callable[[float], None],
    slot_id: int,
    deadline: float | None = None,
) -> bool:
    """Drive Settings → About phone. Returns True only when the page is verified."""
    def remaining() -> float:
        if deadline is None:
            return ABOUT_PHONE_OVERALL_SECONDS
        return deadline - time.monotonic()

    if remaining() < 2.0:
        warnings.append("about_phone_deadline")
        return False
    if samsung:
        logger.info("about_phone_step slot=%s serial=%s step=open_settings", slot_id, serial)
        _start_settings(runner, serial, True, warnings)
        sleep_fn(0.8)
    else:
        logger.info("about_phone_step slot=%s serial=%s step=device_info_intent", slot_id, serial)
        _start_pixel_device_info(runner, serial, warnings)
        sleep_fn(0.8)
        xml = _dump_ui(runner, serial, warnings)
        if ui_is_about_phone(xml):
            logger.info("about_phone_verified slot=%s serial=%s via=intent", slot_id, serial)
            return True
        logger.info("about_phone_step slot=%s serial=%s step=open_settings_fallback", slot_id, serial)
        _start_settings(runner, serial, False, warnings)
        sleep_fn(0.6)

    for attempt in range(MAX_SCROLLS):
        if remaining() < 3.0:
            warnings.append("about_phone_deadline")
            logger.warning("about_phone_deadline slot=%s serial=%s attempt=%s", slot_id, serial, attempt)
            break
        xml = _dump_ui(runner, serial, warnings)
        if ui_is_about_phone(xml):
            logger.info(
                "about_phone_verified slot=%s serial=%s via=ui attempt=%s",
                slot_id,
                serial,
                attempt,
            )
            return True
        if ui_is_settings_home(xml):
            logger.info(
                "about_phone_step slot=%s serial=%s step=settings_home attempt=%s",
                slot_id,
                serial,
                attempt,
            )
        point = find_about_phone_tap(xml)
        if point is not None and not ui_is_about_phone(xml):
            logger.info(
                "about_phone_step slot=%s serial=%s step=tap_about x=%s y=%s",
                slot_id,
                serial,
                point[0],
                point[1],
            )
            _tap(runner, serial, point[0], point[1])
            sleep_fn(1.0)
            xml = _dump_ui(runner, serial, warnings)
            if ui_is_about_phone(xml):
                logger.info("about_phone_verified slot=%s serial=%s via=tap", slot_id, serial)
                return True
            if ui_is_settings_home(xml):
                warnings.append("about_phone_tap_stayed_on_settings_home")
        _swipe_settings_list(runner, serial, xml)
        sleep_fn(0.45)
    warnings.append("about_phone_not_open")
    logger.warning("about_phone_unverified slot=%s serial=%s", slot_id, serial)
    return False


def _read_eid(runner: AdbCommandRunner, serial: str, warnings: list[str]) -> str | None:
    for prop in ("persist.radio.eid", "ril.eid", "ro.ril.eid"):
        try:
            value = runner.run(serial, ["shell", "getprop", prop]).stdout.strip()
        except AdbCommandError:
            continue
        match = _EID_RE.search(value)
        if match:
            return match.group(1)
    try:
        dump = runner.run(serial, ["shell", "dumpsys euicc"]).stdout
    except AdbCommandError as exc:
        warnings.append(f"eid_dumpsys: {exc.__class__.__name__}")
        return None
    match = _EID_RE.search(dump or "")
    return match.group(1) if match else None


def _screencap_png(runner: AdbCommandRunner, serial: str, warnings: list[str]) -> bytes:
    try:
        raw = runner.run_bytes(serial, ["exec-out", "screencap", "-p"])
    except (AdbCommandError, AttributeError) as exc:
        warnings.append(f"screencap: {exc.__class__.__name__}")
        return _MINIMAL_PNG
    if raw.startswith(b"\x89PNG"):
        return raw
    warnings.append("screencap_invalid")
    return _MINIMAL_PNG


def _read_identity(
    runner: AdbCommandRunner,
    serial: str,
    warnings: list[str],
) -> tuple[str | None, str | None, str | None]:
    imei1 = None
    imei2 = None
    try:
        probe = AdbImeiReader(runner).read(serial)
        imei1 = probe.imei1
        imei2 = probe.imei2
        if probe.error:
            warnings.append("imei_probe_incomplete")
    except Exception:
        warnings.append("imei_probe_failed")
    eid = _read_eid(runner, serial, warnings)
    return imei1, imei2, eid


def run_device_about_phone_screenshot(
    *,
    adb_path: str,
    slot_map: dict[int, str],
    request: FarmTaskRequest,
    command_runner: AdbCommandRunner | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> FarmTaskResult:
    slot_id = int(request.farm_slot_id)
    sleeper = sleep_fn or time.sleep
    if not 1 <= slot_id <= MAX_FARM_SLOT_ID:
        return FarmTaskResult(ok=False, http_status=404, error="slot_not_found")
    serial = _serial_for_slot(slot_map, slot_id)
    if not serial:
        return FarmTaskResult(ok=False, http_status=404, error="slot_not_found")
    logger.info("about_phone_start slot=%s serial=%s", slot_id, serial)
    runner = command_runner or AdbCommandRunner(adb_path, timeout_seconds=ABOUT_PHONE_TIMEOUT_SECONDS)
    deadline = time.monotonic() + ABOUT_PHONE_OVERALL_SECONDS
    try:
        state = runner.run(serial, ["get-state"]).stdout.strip()
    except AdbCommandError:
        state = ""
    if state != "device":
        logger.warning("about_phone_offline slot=%s serial=%s", slot_id, serial)
        return FarmTaskResult(ok=False, http_status=409, error="device_offline")

    warnings: list[str] = []
    manufacturer = ""
    model = ""
    try:
        manufacturer = runner.run(serial, ["shell", "getprop", "ro.product.manufacturer"]).stdout.strip()
    except AdbCommandError:
        manufacturer = ""
    try:
        model = runner.run(serial, ["shell", "getprop", "ro.product.model"]).stdout.strip()
    except AdbCommandError:
        model = ""
    samsung = is_samsung_device(slot_id=slot_id, serial=serial, manufacturer=manufacturer)
    samsung_s20 = is_samsung_s20(
        slot_id=slot_id, serial=serial, manufacturer=manufacturer, model=model
    )
    run_best_effort_shell(
        runner,
        serial,
        wake_unlock_commands(samsung_s20=samsung_s20),
        warnings,
        "wake_unlock",
    )
    opened = open_about_phone_page(
        runner,
        serial,
        samsung=samsung,
        warnings=warnings,
        sleep_fn=sleeper,
        slot_id=slot_id,
        deadline=deadline,
    )
    if not opened:
        logger.warning(
            "about_phone_refusing_settings_home slot=%s serial=%s opened=%s",
            slot_id,
            serial,
            opened,
        )
        imei1, imei2, eid = _read_identity(runner, serial, warnings)
        return FarmTaskResult(
            ok=False,
            http_status=422,
            error="about_phone_not_open",
            details={
                "ok": False,
                "slot_id": slot_id,
                "serial": serial,
                "imei1": imei1,
                "imei2": imei2,
                "eid": eid,
                "about_page_verified": False,
                "settings_home": True,
                "warnings": warnings,
            },
        )

    png = _screencap_png(runner, serial, warnings)
    image_b64 = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    logger.info("about_phone_captured slot=%s serial=%s bytes=%s", slot_id, serial, len(png))

    imei1, imei2, eid = _read_identity(runner, serial, warnings)
    logger.info("about_phone_ok slot=%s serial=%s imei1=%s eid=%s", slot_id, serial, bool(imei1), bool(eid))
    details: dict[str, Any] = {
        "ok": True,
        "slot_id": slot_id,
        "serial": serial,
        "imei1": imei1,
        "imei2": imei2,
        "eid": eid,
        "image_base64": image_b64,
        "about_page_verified": True,
        "warnings": warnings,
    }
    return FarmTaskResult(ok=True, http_status=200, details=details)
