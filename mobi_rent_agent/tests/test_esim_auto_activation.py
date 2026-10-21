"""Samsung S20 zero-touch eSIM activation Farm task.

Deep link ``esimmanagerfromqr://launch/?activation_code=<raw LPA>`` verified
live on Slot 21 (RFCNB056WEV, One UI 5.1): QrTransferActivity feeds
INTENT_EXTRA_ACTIVATION_CODE into EsimAddPlanFragment without URL decoding.
No EuiccManager, no provision_esim — public intent plus input taps only.
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
    decode_lpa_activation_code,
    find_confirm_tap,
    lpa_code_is_shell_safe,
    parse_progress_states,
    run_esim_auto_activation,
    smdp_host_from_code,
)
from application.farm_task_types import FarmTaskRequest
from infrastructure.adb_companion import AdbCommandResult, AdbCommandRunner
from tests.test_remote_access_poc import PNG_BYTES, _agent_config

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


def _node(text: str, *, clickable: bool = True, bounds: str = "[100,200][300,400]") -> str:
    flag = "true" if clickable else "false"
    return f"<node text=\"{text}\" class=\"android.widget.Button\" clickable=\"{flag}\" bounds=\"{bounds}\" />"


CONFIRM_UI = _ui(
    _node("Add mobile plan from Mobi Carrier?", clickable=False),
    _node("Add", bounds="[102,2040][978,2148]"),
)
SEARCHING_UI = _ui(_node("Searching for your eSIM...", clickable=False))
FAILED_UI = _ui(
    _node("Can't add eSIM", clickable=False),
    _node("OK"),
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
            index = min(self._ui_index, len(self._ui_polls) - 1)
            self._ui_index += 1
            text = self._ui_polls[index] if self._ui_polls else _ui()
            return AdbCommandResult(stdout=text, stderr="")
        return AdbCommandResult(stdout="", stderr="")

    def commands(self, *prefix: str) -> list[list[str]]:
        return [args for _, args in self.calls if args[: len(prefix)] == list(prefix)]


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
    request = kwargs.pop("request", _req(activation_code=LPA_CODE))
    clock = iter(range(0, 10_000))
    return run_esim_auto_activation(
        adb_path="adb",
        slot_map=slot_map,
        request=request,
        agent_config=config,
        command_runner=runner,
        sleep_fn=lambda _s: None,
        clock=lambda: float(next(clock)),
        poll_interval_seconds=0.0,
        **kwargs,
    )


def test_success_fires_deeplink_taps_confirm_and_reports_states():
    runner = _ActivationRunner(
        logcat_polls=[SEARCHING_LOG, SEARCHING_LOG, DOWNLOADED_LOG],
        ui_polls=[SEARCHING_UI, CONFIRM_UI],
    )
    result = _activate(runner)
    assert result.ok is True, (result.error, result.message)
    assert result.details["activated"] is True
    assert result.details["final_state"] == "DOWNLOAD_COMPLETE"
    assert result.details["state_transitions"][0] == "PROFILE_SEARCHING"
    assert result.details["smdp_address"] == "smdp.example.com"
    assert result.details["taps"] == ["add"]
    starts = runner.commands("shell", "am", "start")
    assert len(starts) == 1
    deeplink = starts[0][-1]
    assert deeplink == f"'{SAMSUNG_ESIM_DEEPLINK_PREFIX}{LPA_CODE}'"
    assert "%24" not in deeplink and "%3A" not in deeplink  # raw, not URL-encoded
    taps = runner.commands("shell", "input", "tap")
    assert taps == [["shell", "input", "tap", "540", "2094"]]
    # The activation code never leaks into the task result.
    assert LPA_CODE not in json.dumps(result.details)
    # Screen returns home afterwards.
    assert ["shell", "input", "keyevent", "3"] in [args for _, args in runner.calls]


def test_not_found_reports_activation_failed():
    runner = _ActivationRunner(logcat_polls=[NOT_FOUND_LOG], ui_polls=[SEARCHING_UI])
    result = _activate(runner)
    assert result.ok is False
    assert result.error == "activation_failed"
    assert result.details["final_state"] == "NOT_FOUND"
    assert result.details["activated"] is False


def test_failure_dialog_without_logcat_state_is_detected():
    runner = _ActivationRunner(logcat_polls=[SEARCHING_LOG], ui_polls=[FAILED_UI])
    result = _activate(runner)
    assert result.ok is False
    assert result.error == "activation_failed"
    assert "can't add esim" in (result.message or "")
    # The failure dialog's OK button is never tapped as a confirm button.
    assert runner.commands("shell", "input", "tap") == []


def test_pixel_bay_is_unsupported_and_untouched():
    runner = _ActivationRunner(manufacturer="Google")
    request = _req(activation_code=LPA_CODE)
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


def test_image_without_qr_reports_decode_failed():
    runner = _ActivationRunner()
    result = _activate(
        runner,
        request=_req(image_base64=__import__("base64").b64encode(PNG_BYTES).decode()),
        decoder=lambda _img: None,
    )
    assert result.ok is False
    assert result.error == "qr_decode_failed"


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
