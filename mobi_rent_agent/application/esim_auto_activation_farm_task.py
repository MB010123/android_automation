"""Farm Agent task: zero-touch eSIM activation on the Samsung Galaxy S20.

Primary method — real One UI flow (verified live on Slot 21, RFCNB056WEV,
One UI 5.1 / Android 13), exactly the screens a human walks:

1. ``am start -n com.samsung.android.app.telephonyui/.netsettings.ui.
   simcardmanager.SimCardMgrActivity`` (exported) — the "SIM manager" screen.
2. Tap "Add eSIM" → "Select your service provider".
3. Tap "Other ways to connect to a mobile network" → "Choose how to add
   your eSIM".
4. Tap "Scan QR code" → ``netsettings.ui.esim.qrcode.CaptureActivity``.
5. Tap the "Open Gallery" button (``id/gallery_button``, bottom right).
6. Samsung Gallery picker ("Select item"): open the "Pictures" album tile,
   tap the newest grid item (``remote_access_place_qr`` pushes the renter QR
   to ``/sdcard/Pictures/`` moments earlier, so it is first).
7. Tap "Done" (``id/menu_edit_app_bar_done``) on the crop screen. The
   scanner decodes the QR natively and One UI walks
   ``OTHER_WAYS_TO_CONNECT_TO_MOBILE_NETWORK → PROFILE_SEARCHING → …``.
8. Monitor ``UT.EsimAddProgressState`` logcat transitions and auto-confirm
   allowlisted dialogs (Add / Confirm / Download / OK / Done).

Every step is verified before the next: dump the UI (``uiautomator dump``),
find the target by label, tap, then poll until the next screen's marker
appears or the step times out (reported as ``ui_flow_step_failed`` with the
step name).

Secondary method — if the payload carries ``activation_code`` explicitly
(smoke tests / operator tooling), the public BROWSABLE deep link
``esimmanagerfromqr://launch/?activation_code=<raw LPA>`` is used instead.

Scope guards: Samsung devices only (Pixel renters keep the manual GADS scan
flow), public intents + ``input tap`` only — no EuiccManager, no
``provision_esim`` companion, no Device Owner, no privileged APIs. Activation
codes never appear in logs or task results (only the SM-DP+ host).
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
    SETUP_UI_AWAITING,
    SETUP_UI_COMPLETED,
    SETUP_UI_WORKING,
    classify_setup_ui,
    public_setup_ui_body,
)
from application.farm_task_types import FarmTaskRequest, FarmTaskResult
from application.remote_access_farm_task import (
    _is_samsung_renter_device,
    _manufacturer_from_adb,
    _place_qr_config_gate,
    delete_placed_esim_qr,
    read_qr_walk_marker,
    write_qr_walk_marker,
)
from infrastructure.adb_companion import AdbCommandError, AdbCommandRunner
from infrastructure.config import AgentConfig

logger = logging.getLogger("farm_agent.esim_auto_activation")

ESIM_AUTO_ACTIVATION_TASK = "esim_auto_activation"

SIM_CARD_MGR_COMPONENT = (
    "com.samsung.android.app.telephonyui/"
    ".netsettings.ui.simcardmanager.SimCardMgrActivity"
)
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

    manufacturer = _manufacturer_from_adb(runner, serial)
    if not _is_samsung_renter_device(slot_id, serial, manufacturer):
        return _result(
            ok=False,
            http_status=501,
            job_id=request.job_id,
            error="automation_unsupported",
            message=NOT_SAMSUNG_MESSAGE,
        )

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
    method = "deeplink" if code else (mode if open_picker else "one_ui_flow")
    leave_on_screen = open_picker
    qr_sha256 = str(request.payload.get("qr_sha256") or "").strip().lower()
    walk_already_done = bool(request.payload.get("qr_walk_completed", False))
    esim_present = bool(request.payload.get("esim_profile_present", False))
    if not walk_already_done:
        marker = read_qr_walk_marker(runner, serial)
        if marker and (not qr_sha256 or qr_sha256 in marker.lower()):
            walk_already_done = True
    if walk_already_done or esim_present:
        deleted = delete_placed_esim_qr(runner, serial)
        logger.info(
            "esim_qr_walk_skipped slot=%s job_id=%s reason=%s",
            slot_id,
            request.job_id,
            "esim_present" if esim_present else "walk_completed",
        )
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
            qr_deleted=bool(deleted),
            setup_ui_state=SETUP_UI_WORKING if not esim_present else SETUP_UI_COMPLETED,
        )
    try:
        runner.run(serial, ["shell", "logcat", "-c"])
        runner.run(serial, ["shell", "input", "keyevent", "224"])  # wake
        runner.run(serial, ["shell", "input", "keyevent", "3"])  # clean start from home
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
                steps_done=steps_done,
                taps=taps,
                sleep_fn=sleep_fn,
                clock=clock,
                poll_interval_seconds=poll_interval_seconds,
                step_timeout_seconds=step_timeout_seconds,
                stop_after="confirm_done" if open_picker else None,
                on_qr_consumed=lambda: delete_placed_esim_qr(runner, serial),
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
                deleted = delete_placed_esim_qr(runner, serial)
                write_qr_walk_marker(runner, serial, qr_sha256 or "1")
                try:
                    ui_xml = ui.dump()
                except AdbCommandError:
                    ui_xml = ""
                classification = classify_setup_ui(
                    ui_xml,
                    walk_completed=True,
                    walk_in_progress=False,
                )
                logger.info(
                    "esim_gated_setup_ok slot=%s job_id=%s steps=%s state=%s",
                    slot_id,
                    request.job_id,
                    steps_done,
                    classification.state,
                )
                if classification.state == SETUP_UI_COMPLETED:
                    leave_on_screen = False
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
                    "qr_deleted": bool(deleted),
                    **public_setup_ui_body(classification),
                }
                if classification.state == SETUP_UI_AWAITING and not classification.prompt:
                    body["setup_ui_state"] = SETUP_UI_AWAITING
                    body["setup_ui_prompt"] = "Please tap 'Add' / 'Confirm' on the screen below"
                if classification.state == SETUP_UI_WORKING and "setup_ui_state" not in body:
                    body["setup_ui_state"] = SETUP_UI_WORKING
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
        # Remove the UI dump. Full activation returns home; open_qr_picker
        # leaves the renter on Gallery so one tap can pick the QR.
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
    runner.run(
        serial,
        [
            "shell",
            "am",
            "start",
            "-n",
            SIM_CARD_MGR_COMPONENT,
            "-e",
            "rootKey",
            "SIMCARD_ESIM_ADD_MOBILE_PLAN",
            "--activity-clear-task",
        ],
    )


def _walk_one_ui_flow(
    ui: _DeviceUi,
    runner: AdbCommandRunner,
    serial: str,
    *,
    steps_done: list[str],
    taps: list[str],
    sleep_fn: Callable[[float], None],
    clock: Callable[[], float],
    poll_interval_seconds: float,
    step_timeout_seconds: float,
    stop_after: str | None = None,
    on_qr_consumed: Callable[[], Any] | None = None,
) -> tuple[str, str] | None:
    """Walk SIM manager → … → Gallery Done. None on success.

    On failure returns ``(step_name, screen_hint)`` where ``screen_hint`` is a
    short note about what the screen showed instead (never raw UI content
    beyond known labels).
    """
    _launch_samsung_sim_manager(runner, serial)
    launch_deadline = clock() + float(step_timeout_seconds)
    ui_xml = ""
    relaunched = False
    while clock() < launch_deadline:
        sleep_fn(float(poll_interval_seconds))
        try:
            ui_xml = ui.dump()
        except AdbCommandError:
            continue
        if is_aosp_sims_screen(ui_xml) and not relaunched:
            # Never stay on AOSP < SIMs>. Re-open One UI SIM Manager once.
            _launch_samsung_sim_manager(runner, serial)
            relaunched = True
            continue
        if ui_contains(ui_xml, "Add eSIM", "Add mobile plan"):
            break
    if not ui_contains(ui_xml, "Add eSIM", "Add mobile plan"):
        return "open_sim_manager", "SIM manager screen did not show Add eSIM"
    steps_done.append("open_sim_manager")

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
