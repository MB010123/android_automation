"""Farm Agent task: zero-touch eSIM activation on the Samsung Galaxy S20.

Samsung One UI (SM-G981U1, Android 13) does not export the AOSP LUI
``PROVISION_EMBEDDED_SUBSCRIPTION`` activity, so the "Add more" entry in
Settings is a dead end for automation. Verified on the live Slot 21 device:
the supported hands-free path is Samsung's public, BROWSABLE deep link

    esimmanagerfromqr://launch/?activation_code=<raw LPA code>

handled by ``com.samsung.android.app.telephonyui/.netsettings.ui.esim.
QrTransferActivity``. It feeds ``INTENT_EXTRA_ACTIVATION_CODE`` straight into
``EsimAddPlanFragment`` → ``searchDownloadableProfileByCode`` (the exact flow
a human reaches by scanning the QR), then One UI walks PROFILE_SEARCHING →
download → confirmation screens. The activation code must be passed RAW:
QrTransferActivity does not URL-decode, so a percent-encoded code is rejected
by the Samsung LPA with "wrong format".

This task:
1. decodes the renter's QR image (same payload shape as
   ``remote_access_place_qr``) into its ``LPA:1$...`` activation code,
2. fires the deep link over ADB,
3. auto-confirms One UI dialogs by text (Add / Confirm / Download / OK), and
4. tracks ``UT.EsimAddProgressState`` logcat transitions as ground truth.

Scope guards: Samsung devices only (Pixel renters keep the manual GADS scan
flow), public viewer intent + ``input tap`` only — no EuiccManager, no
``provision_esim`` companion, no Device Owner, no privileged APIs. The
activation code never appears in logs or task results (only the SM-DP+ host).
"""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from io import BytesIO
from typing import Any, Callable

import requests

from application.farm_task_types import FarmTaskRequest, FarmTaskResult
from application.remote_access_farm_task import (
    MAX_QR_IMAGE_BYTES,
    Downloader,
    _default_downloader,
    _image_extension,
    _inline_image_from_payload,
    _is_samsung_renter_device,
    _manufacturer_from_adb,
    _place_qr_config_gate,
)
from infrastructure.adb_companion import AdbCommandError, AdbCommandRunner
from infrastructure.config import AgentConfig
from infrastructure.esim_qr_security import esim_fetch_url_is_public_https, esim_fetch_url_is_safe

logger = logging.getLogger("farm_agent.esim_auto_activation")

ESIM_AUTO_ACTIVATION_TASK = "esim_auto_activation"

SAMSUNG_ESIM_DEEPLINK_PREFIX = "esimmanagerfromqr://launch/?activation_code="
PROGRESS_LOG_TAG = "UT.EsimAddProgressState"
UI_DUMP_REMOTE_PATH = "/sdcard/mobirent_esim_auto_ui.xml"

# GSMA SGP.22 activation code: LPA:1$<SM-DP+ FQDN>$<matching id>[$<oid>[$...]]
_LPA_CODE_RE = re.compile(
    r"^LPA:1\$(?P<smdp>[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?)\$[A-Za-z0-9\-_.+]*(?:\$[A-Za-z0-9\-_.+]*)*$",
    re.IGNORECASE,
)
_STATE_RE = re.compile(r"put state : [A-Z_]+ -> ([A-Z_]+)")
_BOUNDS_RE = re.compile(r"-?\d+")

# One UI buttons we are allowed to press for the renter (observed texts plus
# conservative synonyms across One UI 5 variants). Matched case-insensitively
# on exact button text.
CONFIRM_BUTTON_TEXTS = frozenset({"add", "confirm", "download", "continue", "ok", "done"})
# The whole screen must look like the eSIM add flow before any tap.
ESIM_SCREEN_MARKERS = ("esim", "mobile plan", "sim")

FAILURE_TEXT_MARKERS = (
    "can't add esim",
    "isn't valid",
    "already been used",
    "couldn't connect",
    "couldn't add",
    "unable to add",
)
SUCCESS_TEXT_MARKERS = (
    "esim added",
    "has been added",
    "added your esim",
    "is ready to use",
)

SUCCESS_STATE_TOKENS = ("DOWNLOADED", "DOWNLOAD_COMPLETE", "INSTALLED", "COMPLETE", "DONE", "SUCCESS")
FAILURE_STATE_TOKENS = ("NOT_FOUND", "ERROR", "FAIL", "CANCEL", "TIMEOUT", "REJECT")

NOT_SAMSUNG_MESSAGE = (
    "automated eSIM activation is only implemented for Samsung One UI bays; "
    "Pixel renters keep the manual GADS scan flow"
)


def decode_lpa_activation_code(image_bytes: bytes) -> str | None:
    """Decode a QR image to its raw LPA activation code, or None.

    Uses zxing-cpp (already a Farm requirement). Never logs the code.
    """
    try:
        import zxingcpp
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise RuntimeError("qr_decoder_unavailable") from exc
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            results = zxingcpp.read_barcodes(image.convert("L"))
    except (OSError, ValueError):
        return None
    for result in results or []:
        text = str(getattr(result, "text", "") or "").strip()
        if _LPA_CODE_RE.match(text):
            return text
    return None


def lpa_code_is_shell_safe(code: str) -> bool:
    """The code is embedded in a single-quoted device-shell argument."""
    if "'" in code or any(ch.isspace() for ch in code):
        return False
    return all(0x20 < ord(ch) < 0x7F for ch in code)


def smdp_host_from_code(code: str) -> str | None:
    match = _LPA_CODE_RE.match(code or "")
    return match.group("smdp") if match else None


def parse_progress_states(logcat_text: str) -> list[str]:
    return _STATE_RE.findall(logcat_text or "")


def _classify_state(state: str) -> str | None:
    upper = (state or "").upper()
    if any(token in upper for token in SUCCESS_STATE_TOKENS):
        return "success"
    if any(token in upper for token in FAILURE_STATE_TOKENS):
        return "failure"
    return None


def _center(bounds: str) -> tuple[int, int] | None:
    nums = [int(v) for v in _BOUNDS_RE.findall(bounds or "")]
    if len(nums) < 4:
        return None
    return (nums[0] + nums[2]) // 2, (nums[1] + nums[3]) // 2


def find_confirm_tap(ui_xml: str) -> tuple[int, int, str] | None:
    """Center of the first allowlisted confirm button on an eSIM screen."""
    try:
        root = ET.fromstring(ui_xml or "")
    except ET.ParseError:
        return None
    full_text = " ".join(
        f"{node.get('text') or ''} {node.get('content-desc') or ''}" for node in root.iter("node")
    ).lower()
    if not any(marker in full_text for marker in ESIM_SCREEN_MARKERS):
        return None
    for node in root.iter("node"):
        if node.get("clickable") != "true":
            continue
        label = str(node.get("text") or "").strip().lower()
        if label not in CONFIRM_BUTTON_TEXTS:
            continue
        center = _center(node.get("bounds") or "")
        if center is None:
            continue
        return center[0], center[1], label
    return None


def ui_failure_marker(ui_xml: str) -> str | None:
    lowered = (ui_xml or "").lower()
    for marker in FAILURE_TEXT_MARKERS:
        if marker in lowered:
            return marker
    return None


def ui_success_marker(ui_xml: str) -> str | None:
    lowered = (ui_xml or "").lower()
    for marker in SUCCESS_TEXT_MARKERS:
        if marker in lowered:
            return marker
    return None


def _result(
    *,
    ok: bool,
    http_status: int,
    job_id: str,
    error: str | None = None,
    message: str | None = None,
    **details: Any,
) -> FarmTaskResult:
    body: dict[str, Any] = {"ok": ok, "job_id": job_id, "error_code": error, "message": message}
    body.update(details)
    return FarmTaskResult(ok=ok, http_status=http_status, error=error, message=message, details=body)


def _qr_image_from_request(
    request: FarmTaskRequest,
    agent_config: AgentConfig,
    downloader: Downloader | None,
) -> bytes | str:
    """Image bytes from image_base64 / esim_qr_url, or an error code string."""
    try:
        inline = _inline_image_from_payload(request.payload)
    except ValueError:
        return "qr_not_an_image"
    if inline is not None:
        payload = inline
    else:
        qr_url = str(request.payload.get("esim_qr_url") or "").strip()
        if not qr_url or not esim_fetch_url_is_public_https(qr_url):
            return "invalid_assignment"
        prefixes = tuple(getattr(agent_config, "remote_access_qr_url_prefixes", ()) or ())
        if prefixes and not esim_fetch_url_is_safe(qr_url, allowed_url_prefixes=prefixes):
            return "invalid_assignment"
        fetch = downloader or _default_downloader
        try:
            payload = fetch(qr_url, float(agent_config.request_timeout_seconds))
        except (requests.RequestException, ValueError, OSError):
            return "qr_download_failed"
    if not payload:
        return "qr_download_failed"
    if len(payload) > MAX_QR_IMAGE_BYTES:
        return "qr_image_too_large"
    if _image_extension(payload) is None:
        return "qr_not_an_image"
    return payload


def run_esim_auto_activation(
    *,
    adb_path: str,
    slot_map: dict[int, str],
    request: FarmTaskRequest,
    agent_config: AgentConfig | None,
    command_runner: AdbCommandRunner | None = None,
    downloader: Downloader | None = None,
    decoder: Callable[[bytes], str | None] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    timeout_seconds: float = 150.0,
    poll_interval_seconds: float = 2.5,
) -> FarmTaskResult:
    slot_id = int(request.farm_slot_id)
    gated = _place_qr_config_gate(agent_config)
    if gated is not None:
        return _result(
            ok=False, http_status=gated.http_status, job_id=request.job_id, error=gated.error
        )
    assert agent_config is not None

    serial = str(slot_map.get(slot_id) or "").strip()
    if not serial:
        return _result(ok=False, http_status=404, job_id=request.job_id, error="slot_not_found")

    runner = command_runner or AdbCommandRunner(adb_path, agent_config.provisioning_timeout_seconds)
    try:
        state = runner.run(serial, ["get-state"]).stdout
    except AdbCommandError:
        state = None
    if state != "device":
        return _result(ok=False, http_status=409, job_id=request.job_id, error="device_offline")

    manufacturer = _manufacturer_from_adb(runner, serial)
    if not _is_samsung_renter_device(slot_id, serial, manufacturer):
        return _result(
            ok=False,
            http_status=501,
            job_id=request.job_id,
            error="automation_unsupported",
            message=NOT_SAMSUNG_MESSAGE,
        )

    code = str(request.payload.get("activation_code") or "").strip()
    if not code:
        image = _qr_image_from_request(request, agent_config, downloader)
        if isinstance(image, str):
            return _result(ok=False, http_status=422, job_id=request.job_id, error=image)
        decode = decoder or decode_lpa_activation_code
        try:
            decoded = decode(image)
        except RuntimeError as exc:
            return _result(ok=False, http_status=503, job_id=request.job_id, error=str(exc))
        if not decoded:
            return _result(
                ok=False,
                http_status=422,
                job_id=request.job_id,
                error="qr_decode_failed",
                message="QR image does not contain an LPA activation code",
            )
        code = decoded
    if not _LPA_CODE_RE.match(code) or not lpa_code_is_shell_safe(code):
        return _result(
            ok=False, http_status=422, job_id=request.job_id, error="invalid_activation_code"
        )
    smdp = smdp_host_from_code(code)

    transitions: list[str] = []
    taps: list[str] = []
    outcome: str | None = None
    detail: str | None = None
    try:
        runner.run(serial, ["shell", "logcat", "-c"])
        runner.run(serial, ["shell", "input", "keyevent", "224"])  # wake
        # QrTransferActivity reads the query value verbatim (no URL decoding),
        # so the LPA code is embedded raw; single quotes keep the device shell
        # from expanding the $ separators.
        runner.run(
            serial,
            [
                "shell",
                "am",
                "start",
                "-a",
                "android.intent.action.VIEW",
                "-d",
                f"'{SAMSUNG_ESIM_DEEPLINK_PREFIX}{code}'",
            ],
        )
        deadline = clock() + float(timeout_seconds)
        while clock() < deadline:
            sleep_fn(float(poll_interval_seconds))
            try:
                log_text = runner.run(
                    serial, ["shell", "logcat", "-d", f"{PROGRESS_LOG_TAG}:I", "*:S"]
                ).stdout
            except AdbCommandError:
                log_text = ""
            states = parse_progress_states(log_text or "")
            for found in states[len(transitions):]:
                transitions.append(found)
            classified = _classify_state(transitions[-1]) if transitions else None
            if classified is not None:
                outcome = classified
                detail = transitions[-1]
                break
            try:
                runner.run(serial, ["shell", "uiautomator", "dump", UI_DUMP_REMOTE_PATH])
                ui_xml = runner.run(serial, ["shell", "cat", UI_DUMP_REMOTE_PATH]).stdout or ""
            except AdbCommandError:
                continue
            failure = ui_failure_marker(ui_xml)
            if failure is not None:
                outcome = "failure"
                detail = failure
                break
            success = ui_success_marker(ui_xml)
            if success is not None:
                outcome = "success"
                detail = success
                break
            tap = find_confirm_tap(ui_xml)
            if tap is not None:
                x, y, label = tap
                taps.append(label)
                runner.run(serial, ["shell", "input", "tap", str(x), str(y)])
    except AdbCommandError:
        logger.warning("esim_auto_activation_adb_failed slot=%s", slot_id)
        return _result(
            ok=False,
            http_status=422,
            job_id=request.job_id,
            error="activation_adb_failed",
            smdp_address=smdp,
            state_transitions=transitions,
            taps=taps,
        )
    finally:
        # Remove the UI dump, dismiss any remaining dialog, and return home.
        for cleanup in (
            ["shell", "rm", "-f", UI_DUMP_REMOTE_PATH],
            ["shell", "input", "keyevent", "4"],
            ["shell", "input", "keyevent", "3"],
        ):
            try:
                runner.run(serial, cleanup)
            except AdbCommandError:
                pass

    if outcome == "success":
        logger.info(
            "esim_auto_activation_ok slot=%s job_id=%s state=%s taps=%s",
            slot_id,
            request.job_id,
            detail,
            len(taps),
        )
        return _result(
            ok=True,
            http_status=200,
            job_id=request.job_id,
            message=f"esim_activation_automated:{detail}",
            smdp_address=smdp,
            final_state=detail,
            state_transitions=transitions,
            taps=taps,
            activated=True,
        )
    error = "activation_failed" if outcome == "failure" else "activation_timeout"
    logger.warning(
        "esim_auto_activation_%s slot=%s job_id=%s detail=%s transitions=%s",
        "failed" if outcome == "failure" else "timeout",
        slot_id,
        request.job_id,
        detail,
        transitions,
    )
    return _result(
        ok=False,
        http_status=422,
        job_id=request.job_id,
        error=error,
        message=detail,
        smdp_address=smdp,
        final_state=detail,
        state_transitions=transitions,
        taps=taps,
        activated=False,
    )
