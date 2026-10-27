"""Farm Agent task: OEM eSIM setup.

Customer Hybrid Guided Flow (``mode=gated_setup`` / ``open_qr_picker``):

1. Wake, dismiss keyguard, and open the OEM eSIM screen even if a profile
   is already installed.
2. Samsung: One UI SIM Manager (``Add eSIM``). Pixel: SIMs / Add SIM via
   ``MANAGE_EMBEDDED_SUBSCRIPTIONS`` (then network/wireless fallbacks).
3. Stop there. Do not auto-click Gallery/Photos or confirmation dialogs.
4. Leave the renter QR in Pictures/Download (media-scanned).

Operator/smoke one-UI walk (Samsung, no gated mode): SIM manager → Add
eSIM → Gallery → Done, then monitor ``UT.EsimAddProgressState``.

Deep-link method: explicit ``activation_code`` → ``esimmanagerfromqr://``
(Samsung only).

Public intents + ``input tap`` only — no EuiccManager, no
``provision_esim`` companion, no Device Owner. Activation codes never
appear in logs or task results (only the SM-DP+ host).
"""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, Callable

from application.esim_setup_ui_state import (
    OEM_PIXEL,
    OEM_SAMSUNG,
    SETUP_UI_AWAITING,
    SETUP_UI_COMPLETED,
    classify_setup_ui,
    handoff_prompt_for_oem,
    public_setup_ui_body,
)
from application.farm_task_types import FarmTaskRequest, FarmTaskResult
from application.remote_access_farm_task import (
    SAMSUNG_ESIM_FALLBACK_INTENT,
    capture_esim_baseline,
    detect_device_oem,
    disable_native_cameras,
    foreground_is_launcher,
    launch_esim_setup_screen,
    read_foreground_component,
    _place_qr_config_gate,
    _tap_through_to_esim_handoff,
    delete_placed_esim_qr,
    read_qr_walk_marker,
    wake_and_unlock_device,
    write_qr_walk_marker,
)
from application.samsung_one_ui import (
    SAMSUNG_SIM_MGR_COMPONENT,
    escape_samsung_game_hijack,
    ui_shows_samsung_game,
)
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

PIXEL_HANDOFF_MARKERS = (
    "Add SIM",
    "Set up an eSIM",
    "Download a SIM instead",
    "Download a SIM",
    "Use a different photo",
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


@dataclass(frozen=True)
class UiFlowStep:
    """One verified step of the One UI walk."""

    name: str
    # Returns tap coordinates for the step target, or None when not found.
    locate: Callable[[str], tuple[int, int] | None]
    # Markers proving the step completed (next screen is showing).
    done_markers: tuple[str, ...]
    # When these markers are already visible, the step is skipped (e.g. some
    # One UI variants go straight to "Choose how to add your eSIM").
    skip_markers: tuple[str, ...] = field(default=())


def _samsung_ui_flow_steps() -> tuple[UiFlowStep, ...]:
    return (
        UiFlowStep(
            name="add_esim",
            locate=lambda xml: find_any_label_tap(xml, ("Add eSIM", "Add mobile plan")),
            done_markers=(
                "Other ways to connect",
                "Choose how to add your eSIM",
                "Scan QR code",
            ),
        ),
        UiFlowStep(
            name="other_ways_to_connect",
            locate=lambda xml: find_label_tap(xml, "Other ways to connect"),
            done_markers=("Scan QR code",),
            skip_markers=("Scan QR code",),
        ),
        UiFlowStep(
            name="scan_qr_code",
            locate=lambda xml: find_any_label_tap(
                xml, ("Scan QR code from service provider", "Scan QR code")
            ),
            done_markers=("Open Gallery", "Choose photo", "Gallery"),
        ),
        UiFlowStep(
            name="open_gallery",
            locate=lambda xml: find_any_label_tap(xml, ("Open Gallery", "Choose photo")),
            done_markers=("Select item", "Albums", "Pictures", "Recent"),
        ),
        UiFlowStep(
            name="open_album",
            locate=find_album_tile_tap,
            done_markers=("Expand",),
        ),
        UiFlowStep(
            name="pick_newest_image",
            locate=find_first_gallery_item_tap,
            done_markers=("Done",),
        ),
        UiFlowStep(
            name="confirm_done",
            locate=lambda xml: find_label_tap(xml, "Done"),
            done_markers=(),  # terminal: the monitor phase takes over
        ),
    )


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


class _DeviceUi:
    """Thin ADB UI helper shared by the step walk and the monitor phase."""

    def __init__(self, runner: AdbCommandRunner, serial: str) -> None:
        self._runner = runner
        self._serial = serial

    def dump(self) -> str:
        self._runner.run(self._serial, ["shell", "uiautomator", "dump", UI_DUMP_REMOTE_PATH])
        return self._runner.run(self._serial, ["shell", "cat", UI_DUMP_REMOTE_PATH]).stdout or ""

    def tap(self, x: int, y: int) -> None:
        self._runner.run(self._serial, ["shell", "input", "tap", str(int(x)), str(int(y))])

    def progress_states(self) -> list[str]:
        try:
            text = self._runner.run(
                self._serial, ["shell", "logcat", "-d", f"{PROGRESS_LOG_TAG}:I", "*:S"]
            ).stdout
        except AdbCommandError:
            return []
        return parse_progress_states(text or "")


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

    # Explicit activation_code → deep-link method (smoke tests / tooling).
    code = str(request.payload.get("activation_code") or "").strip()
    smdp: str | None = None
    if code:
        if not _LPA_CODE_RE.match(code) or not lpa_code_is_shell_safe(code):
            return _result(
                ok=False, http_status=422, job_id=request.job_id, error="invalid_activation_code"
            )
        smdp = smdp_host_from_code(code)

    ui = _DeviceUi(runner, serial)
    transitions: list[str] = []
    taps: list[str] = []
    steps_done: list[str] = []
    outcome: str | None = None
    detail: str | None = None
    mode = str(request.payload.get("mode") or "").strip().lower()
    open_picker = mode in {"open_qr_picker", "open_gallery", "gated_setup"}
    if oem != OEM_SAMSUNG:
        open_picker = True
        code = ""
        smdp = None
    if code:
        method = "deeplink"
    elif open_picker:
        method = mode if mode in {"open_qr_picker", "open_gallery", "gated_setup"} else "gated_setup"
    else:
        method = "one_ui_flow"
    leave_on_screen = open_picker
    qr_sha256 = str(request.payload.get("qr_sha256") or "").strip().lower()
    walk_already_done = bool(request.payload.get("qr_walk_completed", False))
    force_open = bool(request.payload.get("force_open", False))
    if not walk_already_done:
        marker = read_qr_walk_marker(runner, serial)
        if marker and (not qr_sha256 or qr_sha256 in marker.lower()):
            walk_already_done = True
    disable_native_cameras(runner, serial, oem)
    if walk_already_done and not force_open:
        logger.info(
            "esim_qr_walk_skipped slot=%s job_id=%s reason=walk_completed",
            slot_id,
            request.job_id,
        )
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
    try:
        runner.run(serial, ["shell", "logcat", "-c"])
        wake_and_unlock_device(runner, serial)
        if code:
            # QrTransferActivity reads the query value verbatim (no URL
            # decoding); single quotes keep the device shell from expanding
            # the $ separators.
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
        else:
            walk_error = _walk_one_ui_flow(
                ui,
                runner,
                serial,
                oem=oem,
                steps_done=steps_done,
                taps=taps,
                sleep_fn=sleep_fn,
                clock=clock,
                poll_interval_seconds=poll_interval_seconds,
                step_timeout_seconds=step_timeout_seconds,
                stop_after="open_sim_manager" if open_picker else None,
                on_qr_consumed=None if open_picker else (
                    lambda: delete_placed_esim_qr(runner, serial)
                ),
            )
            if walk_error is not None:
                failed_step, screen_hint = walk_error
                return _result(
                    ok=False,
                    http_status=422,
                    job_id=request.job_id,
                    error="ui_flow_step_failed",
                    message=f"step={failed_step}",
                    method=method,
                    failed_step=failed_step,
                    steps_done=steps_done,
                    screen_hint=screen_hint,
                    state_transitions=ui.progress_states(),
                    taps=taps,
                    activated=False,
                    opened=False,
                )
            if open_picker:
                write_qr_walk_marker(runner, serial, qr_sha256 or "1")
                try:
                    ui_xml = ui.dump()
                except AdbCommandError:
                    ui_xml = ""
                classification = classify_setup_ui(
                    ui_xml,
                    walk_completed=True,
                    walk_in_progress=False,
                    oem=oem,
                    esim_newly_added=False,
                )
                component = read_foreground_component(runner, serial)
                if not component or foreground_is_launcher(component):
                    return _result(
                        ok=False,
                        http_status=422,
                        job_id=request.job_id,
                        error="ui_flow_step_failed",
                        message="step=open_sim_manager",
                        method=method,
                        failed_step="open_sim_manager",
                        steps_done=steps_done,
                        screen_hint="foreground is launcher, not eSIM settings",
                        taps=taps,
                        activated=False,
                        opened=False,
                    )
                logger.info(
                    "esim_gated_setup_ok slot=%s job_id=%s steps=%s state=%s oem=%s",
                    slot_id,
                    request.job_id,
                    steps_done,
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
                    "device_oem": oem,
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

        deadline = clock() + float(timeout_seconds)
        while clock() < deadline:
            sleep_fn(float(poll_interval_seconds))
            states = ui.progress_states()
            for found in states[len(transitions):]:
                transitions.append(found)
            classified = _classify_state(transitions[-1]) if transitions else None
            if classified is not None:
                outcome = classified
                detail = transitions[-1]
                break
            try:
                ui_xml = ui.dump()
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
                ui.tap(x, y)
    except AdbCommandError:
        logger.warning("esim_auto_activation_adb_failed slot=%s", slot_id)
        return _result(
            ok=False,
            http_status=422,
            job_id=request.job_id,
            error="activation_adb_failed",
            method=method,
            smdp_address=smdp,
            steps_done=steps_done,
            state_transitions=transitions,
            taps=taps,
        )
    finally:
        # The "Can't add eSIM" dialog survives back/home presses (verified
        # live); it must be dismissed via its OK button or it stays on the
        # renter's screen.
        try:
            final_xml = ui.dump()
            if ui_failure_marker(final_xml):
                dismiss = find_label_tap(final_xml, "OK")
                if dismiss is not None:
                    ui.tap(dismiss[0], dismiss[1])
        except AdbCommandError:
            pass
        # Remove the UI dump. Full activation returns home; hybrid gated
        # setup leaves the renter on SIM Manager for Add eSIM / Gallery.
        cleanup_cmds = [["shell", "rm", "-f", UI_DUMP_REMOTE_PATH]]
        if not leave_on_screen:
            cleanup_cmds.extend(
                [
                    ["shell", "input", "keyevent", "4"],
                    ["shell", "input", "keyevent", "3"],
                ]
            )
        for cleanup in cleanup_cmds:
            try:
                runner.run(serial, cleanup)
            except AdbCommandError:
                pass

    if outcome == "success":
        deleted = delete_placed_esim_qr(runner, serial)
        write_qr_walk_marker(runner, serial, qr_sha256 or "1")
        logger.info(
            "esim_auto_activation_ok slot=%s job_id=%s method=%s state=%s taps=%s",
            slot_id,
            request.job_id,
            method,
            detail,
            len(taps),
        )
        return _result(
            ok=True,
            http_status=200,
            job_id=request.job_id,
            message=f"esim_activation_automated:{detail}",
            method=method,
            smdp_address=smdp,
            final_state=detail,
            steps_done=steps_done,
            state_transitions=transitions,
            taps=taps,
            activated=True,
            qr_walk_completed=True,
            qr_deleted=bool(deleted),
            setup_ui_state=SETUP_UI_COMPLETED,
        )
    error = "activation_failed" if outcome == "failure" else "activation_timeout"
    logger.warning(
        "esim_auto_activation_%s slot=%s job_id=%s method=%s detail=%s transitions=%s",
        "failed" if outcome == "failure" else "timeout",
        slot_id,
        request.job_id,
        method,
        detail,
        transitions,
    )
    return _result(
        ok=False,
        http_status=422,
        job_id=request.job_id,
        error=error,
        message=detail,
        method=method,
        smdp_address=smdp,
        final_state=detail,
        steps_done=steps_done,
        state_transitions=transitions,
        taps=taps,
        activated=False,
    )


def _launch_samsung_sim_manager(runner: AdbCommandRunner, serial: str) -> None:
    launch_esim_setup_screen(runner, serial, OEM_SAMSUNG)


def _escape_game_hijack_and_relaunch(
    runner: AdbCommandRunner, serial: str, ui_xml: str
) -> bool:
    if not ui_shows_samsung_game(ui_xml):
        return False
    escape_samsung_game_hijack(runner, serial)
    _launch_samsung_sim_manager(runner, serial)
    return True


def _pixel_handoff_visible(ui_xml: str) -> bool:
    return ui_contains(ui_xml, *PIXEL_HANDOFF_MARKERS)


def _walk_pixel_handoff(
    ui: _DeviceUi,
    runner: AdbCommandRunner,
    serial: str,
    *,
    steps_done: list[str],
    sleep_fn: Callable[[float], None],
    clock: Callable[[], float],
    poll_interval_seconds: float,
    step_timeout_seconds: float,
) -> tuple[str, str] | None:
    launch_deadline = clock() + float(step_timeout_seconds)
    ui_xml = ""
    launch_esim_setup_screen(runner, serial, OEM_PIXEL)
    while clock() < launch_deadline:
        sleep_fn(float(poll_interval_seconds))
        try:
            ui_xml = ui.dump()
        except AdbCommandError:
            ui_xml = ""
        component = read_foreground_component(runner, serial)
        if not component or foreground_is_launcher(component):
            launch_esim_setup_screen(runner, serial, OEM_PIXEL)
            continue
        if _pixel_handoff_visible(ui_xml):
            steps_done.append("open_sim_manager")
            return None
        _tap_through_to_esim_handoff(runner, serial, OEM_PIXEL)
    return "open_sim_manager", "Pixel SIMs screen did not show Add SIM"


def _walk_one_ui_flow(
    ui: _DeviceUi,
    runner: AdbCommandRunner,
    serial: str,
    *,
    oem: str = OEM_SAMSUNG,
    steps_done: list[str],
    taps: list[str],
    sleep_fn: Callable[[float], None],
    clock: Callable[[], float],
    poll_interval_seconds: float,
    step_timeout_seconds: float,
    stop_after: str | None = None,
    on_qr_consumed: Callable[[], Any] | None = None,
) -> tuple[str, str] | None:
    """Open the OEM eSIM screen, optionally walk Samsung Gallery Done.

    Hybrid gated setup passes ``stop_after='open_sim_manager'`` so the
    customer taps Add eSIM / Add SIM. On failure returns
    ``(step_name, screen_hint)``.
    """
    if oem == OEM_PIXEL or stop_after == "open_sim_manager" and oem != OEM_SAMSUNG:
        return _walk_pixel_handoff(
            ui,
            runner,
            serial,
            steps_done=steps_done,
            sleep_fn=sleep_fn,
            clock=clock,
            poll_interval_seconds=poll_interval_seconds,
            step_timeout_seconds=step_timeout_seconds,
        )
    _launch_samsung_sim_manager(runner, serial)
    launch_deadline = clock() + float(step_timeout_seconds)
    ui_xml = ""
    relaunched = False
    hijack_escapes = 0
    while clock() < launch_deadline:
        sleep_fn(float(poll_interval_seconds))
        try:
            ui_xml = ui.dump()
        except AdbCommandError:
            continue
        if _escape_game_hijack_and_relaunch(runner, serial, ui_xml) and hijack_escapes < 4:
            hijack_escapes += 1
            continue
        if is_aosp_sims_screen(ui_xml) and not relaunched:
            # Never stay on AOSP < SIMs>. Re-open One UI SIM Manager once.
            _launch_samsung_sim_manager(runner, serial)
            relaunched = True
            continue
        if ui_contains(ui_xml, "Add eSIM", "Add mobile plan"):
            break
    if not ui_contains(ui_xml, "Add eSIM", "Add mobile plan"):
        try:
            runner.run(serial, ["shell", "am", "start", "-a", SAMSUNG_ESIM_FALLBACK_INTENT])
        except AdbCommandError:
            pass
        _launch_samsung_sim_manager(runner, serial)
        try:
            ui_xml = ui.dump()
        except AdbCommandError:
            ui_xml = ""
    if not ui_contains(ui_xml, "Add eSIM", "Add mobile plan"):
        return "open_sim_manager", "SIM manager screen did not show Add eSIM"
    component = read_foreground_component(runner, serial)
    if not component or foreground_is_launcher(component):
        return "open_sim_manager", "foreground is launcher, not SIM manager"
    steps_done.append("open_sim_manager")
    if stop_after == "open_sim_manager":
        return None

    for step in _samsung_ui_flow_steps():
        target = step.locate(ui_xml)
        if target is None:
            # One more fresh dump: the previous screen may still be settling.
            try:
                ui_xml = ui.dump()
            except AdbCommandError:
                ui_xml = ""
            target = step.locate(ui_xml)
        if target is None and step.skip_markers and ui_contains(ui_xml, *step.skip_markers):
            # Some One UI variants skip this screen (e.g. no carrier list);
            # the next screen's markers are already visible, so move on.
            steps_done.append(f"{step.name}:skipped")
            continue
        if target is None:
            if ui_failure_marker(ui_xml):
                return step.name, "failure dialog visible"
            return step.name, "target control not found"
        ui.tap(target[0], target[1])
        taps.append(step.name)
        steps_done.append(step.name)
        if on_qr_consumed is not None and step.name in {"pick_newest_image", "confirm_done"}:
            try:
                on_qr_consumed()
            except AdbCommandError:
                pass
        if not step.done_markers:
            return None  # terminal step tapped; monitor phase takes over
        deadline = clock() + float(step_timeout_seconds)
        advanced = False
        while clock() < deadline:
            sleep_fn(float(poll_interval_seconds))
            try:
                ui_xml = ui.dump()
            except AdbCommandError:
                continue
            if _escape_game_hijack_and_relaunch(runner, serial, ui_xml):
                continue
            if ui_failure_marker(ui_xml):
                return step.name, "failure dialog visible"
            if ui_contains(ui_xml, *step.done_markers):
                advanced = True
                break
        if not advanced:
            return step.name, "next screen did not appear"
        if stop_after and step.name == stop_after:
            return None
    return None
