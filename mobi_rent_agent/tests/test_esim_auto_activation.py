"""Samsung S20 eSIM Farm task.

Customer Hybrid Guided Flow (gated_setup / open_qr_picker) opens SIM Manager
and stops. Operator one-UI walk still covers Gallery → Done plus logcat
``UT.EsimAddProgressState``. Deep link: explicit ``activation_code``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest

from application.esim_auto_activation_farm_task import (
    ESIM_AUTO_ACTIVATION_TASK,
    SAMSUNG_ESIM_DEEPLINK_PREFIX,
    SIM_CARD_MGR_COMPONENT,
    UI_DUMP_REMOTE_PATH,
    decode_lpa_activation_code,
    find_album_tile_tap,
    find_confirm_tap,
    find_first_gallery_item_tap,
    find_label_tap,
    is_aosp_sims_screen,
    lpa_code_is_shell_safe,
    parse_progress_states,
    run_esim_auto_activation,
    smdp_host_from_code,
)
from application.remote_access_farm_task import QR_WALK_MARKER_PATH
from application.farm_task_types import FarmTaskRequest
from infrastructure.adb_companion import AdbCommandResult, AdbCommandRunner
from tests.test_remote_access_poc import _agent_config

SAMSUNG_SERIAL = "RFCNB056WEV"
LPA_CODE = "LPA:1$smdp.example.com$MOBIRENT-TEST-TOKEN"

SEARCHING_LOG = "I UT.EsimAddProgressState: put state : UNKNOWN -> PROFILE_SEARCHING\n"
DOWNLOADED_LOG = (
    SEARCHING_LOG
    + "I UT.EsimAddProgressState: put state : PROFILE_SEARCHING -> DOWNLOAD_STARTED\n"
    + "I UT.EsimAddProgressState: put state : DOWNLOAD_STARTED -> DOWNLOAD_COMPLETE\n"
)
NOT_FOUND_LOG = SEARCHING_LOG + "I UT.EsimAddProgressState: put state : PROFILE_SEARCHING -> NOT_FOUND\n"


def _ui(*nodes: str) -> str:
    return "<?xml version='1.0'?><hierarchy>" + "".join(nodes) + "</hierarchy>"


def _node(
    text: str,
    *,
    clickable: bool = True,
    bounds: str = "[100,200][300,400]",
    desc: str = "",
    rid: str = "",
    children: str = "",
) -> str:
    flag = "true" if clickable else "false"
    open_tag = (
        f"<node text=\"{text}\" content-desc=\"{desc}\" resource-id=\"{rid}\" "
        f"class=\"android.widget.Button\" clickable=\"{flag}\" bounds=\"{bounds}\">"
    )
    return open_tag + children + "</node>"


# --- One UI screens, matching the live captures from Slot 21 ---
TUI = "com.samsung.android.app.telephonyui:id/"
GALLERY = "com.sec.android.gallery3d:id/"

SIM_MANAGER_UI = _ui(
    _node("SIM manager", clickable=False),
    _node("Add eSIM", clickable=False, rid=f"{TUI}title", bounds="[72,1001][400,1069]"),
)
PROVIDER_UI = _ui(
    _node("Select your service provider", clickable=False),
    _node("T-Mobile", clickable=False),
    _node(
        "Other ways to connect to a mobile network",
        clickable=False,
        rid=f"{TUI}preference_title",
        bounds="[72,1900][1008,1968]",
    ),
)
CHOOSER_UI = _ui(
    _node("Choose how to add your eSIM", clickable=False),
    _node("Transfer SIM from another device", clickable=False, bounds="[72,1001][860,1069]"),
    _node("Scan QR code", clickable=False, bounds="[72,1169][419,1237]"),
    _node("Search for eSIMs", clickable=False, bounds="[72,1337][491,1405]"),
)
CAPTURE_UI = _ui(
    _node("Scan QR code from your service provider", clickable=False, rid=f"{TUI}qr_guide_text"),
    _node("Enter activation code", rid=f"{TUI}manual_text"),
    _node("", desc="Open Gallery", rid=f"{TUI}gallery_button", bounds="[675,2076][855,2178]"),
)
ALBUMS_UI = _ui(
    _node("Select item", clickable=False),
    _node(
        "",
        rid=f"{GALLERY}recycler_view_item",
        bounds="[741,318][1041,777]",
        children=_node("Pictures", clickable=False, rid=f"{GALLERY}title", bounds="[748,630][856,694]"),
    ),
    # Bottom navigation repeats the album names; those must not be tapped.
    _node("Pictures", bounds="[113,2100][340,2170]"),
    _node("Albums", bounds="[340,2100][567,2170]"),
)
GRID_UI = _ui(
    _node("Select item", clickable=False),
    _node(
        "",
        desc="Button",
        rid=f"{GALLERY}recycler_view_item",
        bounds="[21,318][280,577]",
        children=_node("", desc="Expand", rid=f"{GALLERY}expand", bounds="[196,493][280,577]"),
    ),
    _node(
        "",
        desc="Button",
        rid=f"{GALLERY}recycler_view_item",
        bounds="[287,318][546,577]",
        children=_node("", desc="Expand", rid=f"{GALLERY}expand", bounds="[462,493][546,577]"),
    ),
)
CROP_UI = _ui(
    _node("", clickable=False, desc="Pinch or spread with two fingers to resize.", rid=f"{GALLERY}crop_view"),
    _node("Cancel", rid=f"{GALLERY}menu_edit_app_bar_cancel", bounds="[72,2088][540,2178]"),
    _node("Done", rid=f"{GALLERY}menu_edit_app_bar_done", bounds="[540,2088][1008,2178]"),
)
WALK_UI_POLLS = [SIM_MANAGER_UI, PROVIDER_UI, CHOOSER_UI, CAPTURE_UI, ALBUMS_UI, GRID_UI, CROP_UI]
GAME_LAUNCHER_UI = (
    '<hierarchy><node package="com.samsung.android.game.gamehome" '
    'text="Game Launcher" clickable="true" bounds="[0,0][1080,2400]"/></hierarchy>'
)

CONFIRM_UI = _ui(
    _node("Add mobile plan from Mobi Carrier?", clickable=False),
    _node("Add", bounds="[102,2040][978,2148]"),
)
SEARCHING_UI = _ui(_node("Searching for your eSIM...", clickable=False))
FAILED_UI = _ui(
    _node("Can't add eSIM", clickable=False),
    _node("OK", bounds="[102,2040][978,2148]"),
)


class _ActivationRunner(AdbCommandRunner):
    """Scripted ADB: logcat/ui dump sequences keyed by poll number."""

    def __init__(
        self,
        *,
        manufacturer: str = "samsung",
        logcat_polls: list[str] | None = None,
        ui_polls: list[str] | None = None,
    ) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self._manufacturer = manufacturer
        self._logcat_polls = list(logcat_polls or [])
        self._ui_polls = list(ui_polls or [])
        self._logcat_index = 0
        self._ui_index = 0

    def run(self, serial: str, arguments: list[str]) -> AdbCommandResult:
        self.calls.append((serial, list(arguments)))
        if arguments == ["get-state"]:
            return AdbCommandResult(stdout="device", stderr="")
        if arguments == ["shell", "getprop", "ro.product.manufacturer"]:
            return AdbCommandResult(stdout=self._manufacturer, stderr="")
        if arguments[:3] == ["shell", "logcat", "-d"]:
            index = min(self._logcat_index, len(self._logcat_polls) - 1)
            self._logcat_index += 1
            text = self._logcat_polls[index] if self._logcat_polls else ""
            return AdbCommandResult(stdout=text, stderr="")
        if arguments[:2] == ["shell", "cat"]:
            path = arguments[2] if len(arguments) > 2 else ""
            if path == QR_WALK_MARKER_PATH:
                return AdbCommandResult(stdout="", stderr="")
            if path != UI_DUMP_REMOTE_PATH and not str(path).endswith(".xml"):
                return AdbCommandResult(stdout="", stderr="")
            index = min(self._ui_index, len(self._ui_polls) - 1)
            self._ui_index += 1
            text = self._ui_polls[index] if self._ui_polls else _ui()
            return AdbCommandResult(stdout=text, stderr="")
        return AdbCommandResult(stdout="", stderr="")

    def commands(self, *prefix: str) -> list[list[str]]:
        return [args for _, args in self.calls if args[: len(prefix)] == list(prefix)]

    def tap_points(self) -> list[tuple[int, int]]:
        return [(int(args[3]), int(args[4])) for args in self.commands("shell", "input", "tap")]


def _req(**payload: Any) -> FarmTaskRequest:
    body: dict[str, Any] = {"rental_id": "1dd479bd-a3a3-5220-87c9-bae7da81578a", "actor": "renter"}
    body.update(payload)
    return FarmTaskRequest(
        job_id="7e5bdc7f-aaaa-bbbb-cccc-dddddddddddd",
        task_type=ESIM_AUTO_ACTIVATION_TASK,
        farm_slot_id=21,
        payload=body,
    )


def _activate(runner: AdbCommandRunner, **kwargs: Any):
    config = kwargs.pop("agent_config", _agent_config(remote_access_poc_enabled=True))
    slot_map = kwargs.pop("slot_map", {21: SAMSUNG_SERIAL})
    request = kwargs.pop("request", _req())
    clock = iter(range(0, 100_000))
    return run_esim_auto_activation(
        adb_path="adb",
        slot_map=slot_map,
        request=request,
        agent_config=config,
        command_runner=runner,
        sleep_fn=lambda _s: None,
        clock=lambda: float(next(clock)),
        poll_interval_seconds=0.0,
        step_timeout_seconds=kwargs.pop("step_timeout_seconds", 6.0),
        **kwargs,
    )


def test_open_qr_picker_stops_at_sim_manager_and_keeps_qr():
    runner = _ActivationRunner(ui_polls=[SIM_MANAGER_UI, SIM_MANAGER_UI])
    result = _activate(runner, request=_req(mode="open_qr_picker"))
    assert result.ok is True, (result.error, result.message)
    assert result.details["method"] == "open_qr_picker"
    assert result.details["opened"] is True
    assert result.details["activated"] is False
    assert result.details["qr_walk_completed"] is True
    assert result.details["qr_deleted"] is False
    assert result.details["setup_ui_state"] == "awaiting_human_click"
    assert "Add eSIM" in str(result.details.get("setup_ui_prompt") or "")
    assert result.details["steps_done"] == ["open_sim_manager"]
    assert result.details["taps"] == []
    rms = [args for _, args in runner.calls if args[:3] == ["shell", "rm", "-f"]]
    assert not any(str(args[-1]).endswith("esim_qr.png") for args in rms)
    dismiss = " ".join(" ".join(args) for _, args in runner.calls)
    assert "dismiss-keyguard" in dismiss
    # Wake/home happen at the start; cleanup must not send home again.
    homes = [args for _, args in runner.calls if args == ["shell", "input", "keyevent", "3"]]
    assert len(homes) == 1
    assert SIM_CARD_MGR_COMPONENT in runner.commands("shell", "am", "start")[0]
    aosp = " ".join(" ".join(args) for _, args in runner.calls)
    assert "SIM_CARD_SETTINGS" not in aosp
    assert "MANAGE_ALL_SIM_PROFILES_SETTINGS" not in aosp
    assert runner.tap_points() == []


def test_qr_walk_escapes_game_launcher_and_opens_sim_manager():
    runner = _ActivationRunner(ui_polls=[GAME_LAUNCHER_UI, SIM_MANAGER_UI, SIM_MANAGER_UI])
    result = _activate(runner, request=_req(mode="open_qr_picker"))
    assert result.ok is True, (result.error, result.message)
    joined = " ".join(" ".join(args) for _, args in runner.calls)
    assert "am force-stop com.samsung.android.game.gamehome" in joined
    assert "KEYCODE_HOME" in joined
    starts = runner.commands("shell", "am", "start")
    assert starts
    assert SIM_CARD_MGR_COMPONENT in starts[0]
    assert "android.settings.SETTINGS" not in joined
    assert "MANAGE_ALL_SIM_PROFILES_SETTINGS" not in joined


def test_qr_walk_is_skipped_when_already_completed():
    runner = _ActivationRunner(ui_polls=WALK_UI_POLLS)
    result = _activate(runner, request=_req(mode="gated_setup", qr_walk_completed=True))
    assert result.ok is True
    assert result.details["skipped"] is True
    assert result.details["qr_walk_completed"] is True
    assert result.details["qr_deleted"] is False
    assert result.details["setup_ui_state"] == "awaiting_human_click"
    assert runner.commands("shell", "am", "start") == []
    assert runner.tap_points() == []
    rms = [args for _, args in runner.calls if args[:3] == ["shell", "rm", "-f"]]
    assert not any(str(args[-1]).endswith("esim_qr.png") for args in rms)


def test_aosp_sims_fragment_is_detected():
    xml = _ui(_node("SIMs", clickable=False), _node("DOWNLOADED SIM"), _node("+ Add more"))
    assert is_aosp_sims_screen(xml) is True
    assert is_aosp_sims_screen(SIM_MANAGER_UI) is False


def test_ui_walk_success_taps_every_screen_and_reports_states():
    runner = _ActivationRunner(
        logcat_polls=[SEARCHING_LOG, DOWNLOADED_LOG],
        ui_polls=WALK_UI_POLLS + [CONFIRM_UI],
    )
    result = _activate(runner)
    assert result.ok is True, (result.error, result.message)
    assert result.details["method"] == "one_ui_flow"
    assert result.details["activated"] is True
    assert result.details["final_state"] == "DOWNLOAD_COMPLETE"
    assert result.details["steps_done"] == [
        "open_sim_manager",
        "add_esim",
        "other_ways_to_connect",
        "scan_qr_code",
        "open_gallery",
        "open_album",
        "pick_newest_image",
        "confirm_done",
    ]
    # SIM manager launched directly via the exported activity, no deep link.
    starts = runner.commands("shell", "am", "start")
    assert len(starts) == 1 and SIM_CARD_MGR_COMPONENT in starts[0]
    # Taps land on the real controls: Add eSIM row, Other ways, Scan QR code,
    # gallery button, Pictures album tile (not the bottom tab), first image,
    # Done, then the Add confirm dialog.
    assert runner.tap_points() == [
        (236, 1035),   # Add eSIM
        (540, 1934),   # Other ways to connect
        (245, 1203),   # Scan QR code
        (765, 2127),   # Open Gallery
        (891, 547),    # Pictures album tile
        (150, 447),    # newest image in grid
        (774, 2133),   # Gallery crop Done
        (540, 2094),   # Add (confirm dialog)
    ]
    assert result.details["taps"][-1] == "add"
    # Cleanup returns home.
    assert ["shell", "input", "keyevent", "3"] in [args for _, args in runner.calls]


def test_ui_walk_skips_provider_screen_when_absent():
    # Some One UI variants jump straight from Add eSIM to the chooser.
    polls = [SIM_MANAGER_UI, CHOOSER_UI, CHOOSER_UI, CAPTURE_UI, ALBUMS_UI, GRID_UI, CROP_UI]
    runner = _ActivationRunner(logcat_polls=[DOWNLOADED_LOG], ui_polls=polls)
    result = _activate(runner)
    assert result.ok is True, (result.error, result.message)
    assert "other_ways_to_connect:skipped" in result.details["steps_done"]


def test_ui_walk_step_failure_reports_step_name():
    # The chooser never appears after tapping "Other ways to connect".
    polls = [SIM_MANAGER_UI, PROVIDER_UI, PROVIDER_UI]
    runner = _ActivationRunner(logcat_polls=[""], ui_polls=polls)
    result = _activate(runner)
    assert result.ok is False
    assert result.error == "ui_flow_step_failed"
    assert result.details["failed_step"] == "other_ways_to_connect"
    assert result.details["steps_done"][-1] == "other_ways_to_connect"


def test_ui_walk_failure_dialog_mid_walk_aborts():
    polls = [SIM_MANAGER_UI, PROVIDER_UI, CHOOSER_UI, FAILED_UI]
    runner = _ActivationRunner(logcat_polls=[""], ui_polls=polls)
    result = _activate(runner)
    assert result.ok is False
    assert result.error == "ui_flow_step_failed"
    assert result.details["screen_hint"] == "failure dialog visible"


def test_ui_walk_not_found_reports_activation_failed():
    runner = _ActivationRunner(
        logcat_polls=[SEARCHING_LOG, NOT_FOUND_LOG],
        ui_polls=WALK_UI_POLLS + [SEARCHING_UI],
    )
    result = _activate(runner)
    assert result.ok is False
    assert result.error == "activation_failed"
    assert result.details["final_state"] == "NOT_FOUND"
    assert result.details["activated"] is False


def test_ui_walk_failure_dialog_after_done_is_detected():
    runner = _ActivationRunner(
        logcat_polls=[SEARCHING_LOG],
        ui_polls=WALK_UI_POLLS + [FAILED_UI],
    )
    result = _activate(runner)
    assert result.ok is False
    assert result.error == "activation_failed"
    assert "can't add esim" in (result.message or "")
    # The failure dialog's OK button is never tapped as a *confirm* action
    # (no confirm labels recorded), but cleanup taps it once to dismiss the
    # dialog, which survives back/home presses on One UI.
    assert result.details["taps"] == [
        "add_esim",
        "other_ways_to_connect",
        "scan_qr_code",
        "open_gallery",
        "open_album",
        "pick_newest_image",
        "confirm_done",
    ]
    assert runner.tap_points().count((540, 2094)) == 1  # cleanup dismissal


def test_deeplink_method_used_for_explicit_activation_code():
    runner = _ActivationRunner(
        logcat_polls=[SEARCHING_LOG, SEARCHING_LOG, DOWNLOADED_LOG],
        ui_polls=[SEARCHING_UI, CONFIRM_UI],
    )
    result = _activate(runner, request=_req(activation_code=LPA_CODE))
    assert result.ok is True, (result.error, result.message)
    assert result.details["method"] == "deeplink"
    assert result.details["activated"] is True
    assert result.details["smdp_address"] == "smdp.example.com"
    assert result.details["taps"] == ["add"]
    starts = runner.commands("shell", "am", "start")
    assert len(starts) == 1
    deeplink = starts[0][-1]
    assert deeplink == f"'{SAMSUNG_ESIM_DEEPLINK_PREFIX}{LPA_CODE}'"
    assert "%24" not in deeplink and "%3A" not in deeplink  # raw, not URL-encoded
    # The activation code never leaks into the task result.
    assert LPA_CODE not in json.dumps(result.details)


def test_pixel_bay_is_unsupported_and_untouched():
    runner = _ActivationRunner(manufacturer="Google")
    request = _req()
    request.farm_slot_id = 1
    result = _activate(runner, slot_map={1: "18171FDF6005WG"}, request=request)
    assert result.ok is False
    assert result.http_status == 501
    assert result.error == "automation_unsupported"
    assert runner.commands("shell", "am", "start") == []
    assert runner.commands("shell", "input") == []


def test_invalid_or_shell_unsafe_code_is_rejected():
    for bad in ("not-an-lpa-code", "LPA:1$smdp$a'b", "LPA:1$smdp$a b", "LPA:2$smdp$x"):
        runner = _ActivationRunner()
        result = _activate(runner, request=_req(activation_code=bad))
        assert result.ok is False, bad
        assert result.error == "invalid_activation_code"
        assert runner.commands("shell", "am", "start") == []


def test_qr_round_trip_decode():
    zxingcpp = pytest.importorskip("zxingcpp")
    from PIL import Image

    try:
        bitmap = zxingcpp.write_barcode(zxingcpp.BarcodeFormat.QRCode, LPA_CODE, width=300, height=300)
        height, width = bitmap.shape
        image = Image.frombuffer("L", (width, height), bytes(memoryview(bitmap)), "raw", "L", 0, 1)
    except (AttributeError, TypeError, ValueError) as exc:  # pragma: no cover - zxing build variants
        pytest.skip(f"zxingcpp QR encode unavailable: {exc}")
    from io import BytesIO

    buf = BytesIO()
    image.save(buf, format="PNG")
    assert decode_lpa_activation_code(buf.getvalue()) == LPA_CODE


def test_ui_finders():
    # Album tile: the grid item wins over the bottom navigation tab.
    assert find_album_tile_tap(ALBUMS_UI) == (891, 547)
    # First (newest) image tile in the grid.
    assert find_first_gallery_item_tap(GRID_UI) == (150, 447)
    # Album tiles (with a title child) are not mistaken for image tiles.
    assert find_first_gallery_item_tap(ALBUMS_UI) is None
    # Label finder matches text and content-desc.
    assert find_label_tap(CAPTURE_UI, "Open Gallery") == (765, 2127)
    assert find_label_tap(CROP_UI, "Done") == (774, 2133)
    assert find_label_tap(SIM_MANAGER_UI, "Add eSIM") == (236, 1035)
    assert find_label_tap(CHOOSER_UI, "nonexistent") is None


def test_helpers():
    assert smdp_host_from_code(LPA_CODE) == "smdp.example.com"
    assert lpa_code_is_shell_safe(LPA_CODE) is True
    assert lpa_code_is_shell_safe("LPA:1$s$a'b") is False
    assert parse_progress_states(DOWNLOADED_LOG) == [
        "PROFILE_SEARCHING",
        "DOWNLOAD_STARTED",
        "DOWNLOAD_COMPLETE",
    ]
    assert find_confirm_tap(CONFIRM_UI) == (540, 2094, "add")
    # Confirm buttons outside an eSIM-looking screen are ignored.
    assert find_confirm_tap(_ui(_node("OK"))) is None
    # Non-allowlisted buttons are never tapped.
    assert find_confirm_tap(_ui(_node("eSIM settings", clickable=False), _node("Erase SIM"))) is None
