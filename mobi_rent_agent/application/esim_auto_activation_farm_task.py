"""Farm Agent task: OEM eSIM setup.

Pure Hybrid Manual Model:

1. Wake the phone and open the OEM SIM/eSIM settings screen even if a
   profile is already installed.
2. Samsung: SIM Manager. Pixel: ``android.settings.WIRELESS_SETTINGS``
   (Network & internet / SIMs).
3. Stop immediately. Never tap Add eSIM / Add SIM, never open Gallery,
   never deep-link an LPA code, never call EuiccManager / ``provision_esim``.
4. Set ``setup_ui_state=awaiting_human_click``. The renter finishes setup.

QR image push and media scan happen in ``remote_access_place_qr``. Camera
packages are disabled here and there so the renter must use Gallery/Photos.
"""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from io import BytesIO
from typing import Any, Callable

from application.esim_setup_ui_state import (
    OEM_PIXEL,
    OEM_SAMSUNG,
    SETUP_UI_AWAITING,
    classify_setup_ui,
    handoff_prompt_for_oem,
    public_setup_ui_body,
)
from application.farm_task_types import FarmTaskRequest, FarmTaskResult
from application.remote_access_farm_task import (
    capture_esim_baseline,
    detect_device_oem,
    disable_native_cameras,
    launch_esim_setup_screen,
    _place_qr_config_gate,
    read_qr_walk_marker,
    wake_and_unlock_device,
    write_qr_walk_marker,
)
from application.samsung_one_ui import SAMSUNG_SIM_MGR_COMPONENT
from infrastructure.adb_companion import AdbCommandError, AdbCommandRunner
from infrastructure.config import AgentConfig

logger = logging.getLogger("farm_agent.esim_auto_activation")

ESIM_AUTO_ACTIVATION_TASK = "esim_auto_activation"

SIM_CARD_MGR_COMPONENT = SAMSUNG_SIM_MGR_COMPONENT
SAMSUNG_ESIM_DEEPLINK_PREFIX = "esimmanagerfromqr://launch/?activation_code="
PROGRESS_LOG_TAG = "UT.EsimAddProgressState"
UI_DUMP_REMOTE_PATH = "/sdcard/mobirent_esim_auto_ui.xml"
GALLERY_ITEM_RID = "recycler_view_item"
GALLERY_DONE_RID = "menu_edit_app_bar_done"

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
# The whole screen must look like the eSIM add flow before any confirm tap.
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


def _parse_ui(ui_xml: str) -> ET.Element | None:
    try:
        return ET.fromstring(ui_xml or "")
    except ET.ParseError:
        return None


def _node_label(node: ET.Element) -> str:
    return f"{node.get('text') or ''} {node.get('content-desc') or ''}".strip()


def ui_contains(ui_xml: str, *markers: str) -> bool:
    lowered = (ui_xml or "").lower()
    return any(marker.lower() in lowered for marker in markers)


def find_label_tap(ui_xml: str, label: str) -> tuple[int, int] | None:
    """Center of the first node whose text/content-desc contains ``label``."""
    root = _parse_ui(ui_xml)
    if root is None:
        return None
    needle = label.lower()
    for node in root.iter("node"):
        if needle in _node_label(node).lower():
            return _center(node.get("bounds") or "")
    return None


def find_any_label_tap(ui_xml: str, labels: tuple[str, ...]) -> tuple[int, int] | None:
    for label in labels:
        hit = find_label_tap(ui_xml, label)
        if hit is not None:
            return hit
    return None


def is_aosp_sims_screen(ui_xml: str) -> bool:
    """True for the broken AOSP ``< SIMs>`` fragment on One UI."""
    lowered = (ui_xml or "").lower()
    if "add esim" in lowered or "sim manager" in lowered:
        return False
    return "downloaded sim" in lowered and "add more" in lowered


def find_album_tile_tap(ui_xml: str, titles: tuple[str, ...] = ("Pictures", "Recent")) -> tuple[int, int] | None:
    """Center of the Gallery album tile (recycler item) matching a title.

    The bottom navigation bar repeats the album names, so only grid tiles
    (``id/recycler_view_item``) whose subtree holds the title count.
    """
    root = _parse_ui(ui_xml)
    if root is None:
        return None
    wanted = tuple(t.lower() for t in titles)
    for title in wanted:
        for node in root.iter("node"):
            rid = node.get("resource-id") or ""
            if not rid.endswith(GALLERY_ITEM_RID):
                continue
            subtree_text = " ".join(
                (child.get("text") or "") for child in node.iter("node")
            ).lower()
            if title in subtree_text:
                return _center(node.get("bounds") or "")
    return None


def find_first_gallery_item_tap(ui_xml: str) -> tuple[int, int] | None:
    """Center of the first (newest) image tile in the Gallery grid.

    Image tiles are clickable ``id/recycler_view_item`` nodes without album
    title text in their subtree.
    """
    root = _parse_ui(ui_xml)
    if root is None:
        return None
    for node in root.iter("node"):
        rid = node.get("resource-id") or ""
        if not rid.endswith(GALLERY_ITEM_RID) or node.get("clickable") != "true":
            continue
        has_title = any(
            (child.get("resource-id") or "").endswith("title") and (child.get("text") or "").strip()
            for child in node.iter("node")
        )
        if not has_title:
            return _center(node.get("bounds") or "")
    return None


def find_confirm_tap(ui_xml: str) -> tuple[int, int, str] | None:
    """Center of the first allowlisted confirm button on an eSIM screen."""
    root = _parse_ui(ui_xml)
    if root is None:
        return None
    full_text = " ".join(_node_label(node) for node in root.iter("node")).lower()
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


def run_esim_auto_activation(
    *,
    adb_path: str,
    slot_map: dict[int, str],
    request: FarmTaskRequest,
    agent_config: AgentConfig | None,
    command_runner: AdbCommandRunner | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    timeout_seconds: float = 180.0,
    poll_interval_seconds: float = 2.5,
    step_timeout_seconds: float = 25.0,
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

    oem = detect_device_oem(runner, serial, slot_id)

    code = str(request.payload.get("activation_code") or "").strip()
    if code:
        if not _LPA_CODE_RE.match(code) or not lpa_code_is_shell_safe(code):
            return _result(
                ok=False, http_status=422, job_id=request.job_id, error="invalid_activation_code"
            )
        # Hybrid model never deep-links or provisions the profile.

    transitions: list[str] = []
    taps: list[str] = []
    steps_done: list[str] = []
    mode = str(request.payload.get("mode") or "").strip().lower()
    method = mode if mode in {"open_qr_picker", "open_gallery", "gated_setup"} else "gated_setup"
    qr_sha256 = str(request.payload.get("qr_sha256") or "").strip().lower()
    walk_already_done = bool(request.payload.get("qr_walk_completed", False))
    force_open = bool(request.payload.get("force_open", False))
    if not walk_already_done:
        marker = read_qr_walk_marker(runner, serial)
        if marker and (not qr_sha256 or qr_sha256 in marker.lower()):
            walk_already_done = True
    if walk_already_done and not force_open:
        prompt = handoff_prompt_for_oem(oem)
        return _result(
            ok=True,
            http_status=200,
            job_id=request.job_id,
            message="esim_qr_walk_skipped",
            method=method,
            steps_done=[],
            taps=[],
            activated=False,
            opened=False,
            skipped=True,
            qr_walk_completed=True,
            qr_deleted=False,
            device_oem=oem,
            setup_ui_state=SETUP_UI_AWAITING,
            setup_ui_prompt=prompt,
            setup_human_click="Add SIM" if oem == OEM_PIXEL else "Add eSIM",
        )
    disable_native_cameras(runner, serial, oem)
    try:
        wake_and_unlock_device(runner, serial)
        launched = launch_esim_setup_screen(runner, serial, oem)
        steps_done.append("open_sim_manager")
        write_qr_walk_marker(runner, serial, qr_sha256 or "1")
        classification = classify_setup_ui(
            None,
            None,
            walk_completed=True,
            walk_in_progress=False,
            oem=oem,
            esim_newly_added=False,
        )
        logger.info(
            "esim_gated_setup_ok slot=%s job_id=%s launched=%s state=%s oem=%s",
            slot_id,
            request.job_id,
            launched,
            classification.state,
            oem,
        )
        body = {
            "ok": True,
            "http_status": 200,
            "job_id": request.job_id,
            "message": "esim_gated_setup_ready",
            "method": method,
            "steps_done": steps_done,
            "taps": taps,
            "activated": False,
            "opened": True,
            "qr_walk_completed": True,
            "qr_deleted": False,
            "skipped": bool(walk_already_done and not force_open),
            "device_oem": oem,
            "esim_setup_launched": launched,
            **public_setup_ui_body(classification),
            **capture_esim_baseline(runner, serial),
        }
        body["setup_ui_state"] = SETUP_UI_AWAITING
        body["setup_ui_prompt"] = classification.prompt or handoff_prompt_for_oem(oem)
        body["setup_human_click"] = (
            classification.human_click_label
            or ("Add SIM" if oem == OEM_PIXEL else "Add eSIM")
        )
        return _result(**body)
    except AdbCommandError:
        logger.warning("esim_auto_activation_adb_failed slot=%s", slot_id)
        return _result(
            ok=False,
            http_status=422,
            job_id=request.job_id,
            error="activation_adb_failed",
            method=method,
            steps_done=steps_done,
            state_transitions=transitions,
            taps=taps,
        )

