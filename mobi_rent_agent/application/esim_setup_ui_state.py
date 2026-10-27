"""Spinner-gated eSIM setup UI: working vs awaiting_human_click vs completed.

Public intents and uiautomator text only. Never EuiccManager, never logs
activation codes.
"""
from __future__ import annotations

from dataclasses import dataclass

SETUP_UI_WORKING = "working"
SETUP_UI_AWAITING = "awaiting_human_click"
SETUP_UI_COMPLETED = "completed"
SETUP_UI_STATES = frozenset({SETUP_UI_WORKING, SETUP_UI_AWAITING, SETUP_UI_COMPLETED})
SETUP_WORKING_FALLBACK_SECONDS = 45
SETUP_TIMEOUT_BANNER = (
    "Automatic setup timed out after 45 seconds. "
    "The phone screen has been unlocked so you can complete your eSIM setup manually."
)

DEFAULT_HUMAN_PROMPT = "Please tap 'Add' / 'Confirm' on the screen below"
GALLERY_HANDOFF_PROMPT = (
    "Your QR code is saved to the phone's Gallery! On the screen below, tap 'Add eSIM', "
    "choose 'Scan QR code' -> 'Gallery', and select the QR image at the top to finish."
)
SIM_MANAGER_HANDOFF_PROMPT = GALLERY_HANDOFF_PROMPT
SETUP_COMPLETED_BANNER = "eSIM Activated!"

WORKING_TEXT_MARKERS = (
    "checking network info",
    "finding service provider",
    "adding esim",
    "downloading",
    "activating",
    "searching for your esim",
    "searching for esim",
    "please wait",
    "setting up",
    "connecting to the network",
    "profile searching",
    "download started",
    "looking for",
)

COMPLETED_TEXT_MARKERS = (
    "esim added",
    "has been added",
    "added your esim",
    "is ready to use",
    "mobile plan added",
    "successfully added",
)

# Confirmation copy. Not the SIM Manager "Add eSIM" row.
AWAITING_PROMPT_MARKERS = (
    "add this mobile plan",
    "add mobile plan from",
    "download this sim",
    "download this mobile plan",
    "use this mobile plan",
    "turn on this mobile plan",
    "turn on this sim",
    "add this esim",
)

AWAITING_BUTTONS = frozenset({"add", "download", "confirm", "turn on", "ok"})
GALLERY_DONE_MARKERS = ("done",)
# Hybrid Guided Flow: SIM Manager is the customer handoff screen.
SIM_MANAGER_MARKERS = ("sim manager", "add esim")


@dataclass(frozen=True)
class SetupUiClassification:
    state: str
    prompt: str | None = None
    human_click_label: str | None = None
    reason: str = "unknown"


@dataclass(frozen=True)
class SetupSurfaceDecision:
    """Customer 'Set up your phone' overlay: spinner vs live canvas."""

    show_spinner: bool
    show_phone: bool
    controls_enabled: bool
    timed_out: bool
    banner: str | None = None
    show_retry: bool = False


def decide_setup_surface(
    *,
    setup_ui_state: str | None = None,
    elapsed_working_seconds: float = 0.0,
    setup_mode: bool = False,
    server_manual_fallback: bool = False,
    automated_setup_timed_out: bool = False,
    human_handoff_unlocked: bool = False,
) -> SetupSurfaceDecision:
    """Hide the phone on ``working`` until 45s elapse (or the farm walk fails).

    ``awaiting_human_click`` unlocks immediately. Once the customer has been
    handed off, later ``working`` polls must not re-lock the canvas.
    """
    if not setup_mode:
        return SetupSurfaceDecision(
            show_spinner=False,
            show_phone=True,
            controls_enabled=True,
            timed_out=False,
        )
    state = str(setup_ui_state or "").strip()
    completed = state == SETUP_UI_COMPLETED
    awaiting = state == SETUP_UI_AWAITING or (bool(human_handoff_unlocked) and not completed)
    if completed or awaiting:
        return SetupSurfaceDecision(
            show_spinner=False,
            show_phone=True,
            controls_enabled=True,
            timed_out=False,
        )
    working = state == SETUP_UI_WORKING
    timed_out = working and (
        bool(server_manual_fallback)
        or bool(automated_setup_timed_out)
        or float(elapsed_working_seconds or 0) >= SETUP_WORKING_FALLBACK_SECONDS
    )
    if working and not timed_out:
        return SetupSurfaceDecision(
            show_spinner=True,
            show_phone=False,
            controls_enabled=False,
            timed_out=False,
        )
    return SetupSurfaceDecision(
        show_spinner=False,
        show_phone=True,
        controls_enabled=True,
        timed_out=timed_out,
        banner=SETUP_TIMEOUT_BANNER if timed_out else None,
        show_retry=timed_out,
    )


def classify_setup_ui(
    ui_xml: str | None,
    activity: str | None = None,
    *,
    walk_completed: bool = False,
    walk_in_progress: bool = False,
    activation_confirmed: bool = False,
    esim_profile_present: bool = False,
) -> SetupUiClassification:
    """Map a live screen to spinner / reveal-phone / finished.

    Hybrid Guided Flow: SIM Manager with Add eSIM is ``awaiting_human_click``
    so the customer can tap through Gallery and confirm. ``walk_in_progress``
    still hides Gallery only when an operator one-UI walk is actively tapping.
    """
    if activation_confirmed or esim_profile_present:
        return SetupUiClassification(SETUP_UI_COMPLETED, reason="esim_installed")
    if walk_in_progress:
        return SetupUiClassification(SETUP_UI_WORKING, reason="walk_in_progress")
    lowered = f"{ui_xml or ''} {activity or ''}".lower()
    if not lowered.strip():
        if walk_completed:
            return SetupUiClassification(
                SETUP_UI_AWAITING,
                prompt=SIM_MANAGER_HANDOFF_PROMPT,
                human_click_label="Add eSIM",
                reason="post_walk_unknown",
            )
        return SetupUiClassification(SETUP_UI_WORKING, reason="ui_unknown")
    if any(marker in lowered for marker in COMPLETED_TEXT_MARKERS):
        return SetupUiClassification(SETUP_UI_COMPLETED, reason="success_copy")
    if any(marker in lowered for marker in WORKING_TEXT_MARKERS):
        return SetupUiClassification(SETUP_UI_WORKING, reason="progress_copy")
    awaiting = _awaiting_human(lowered)
    if awaiting is not None:
        return awaiting
    if walk_completed:
        return SetupUiClassification(
            SETUP_UI_AWAITING,
            prompt=SIM_MANAGER_HANDOFF_PROMPT,
            human_click_label="Add eSIM",
            reason="post_walk_handoff",
        )
    return SetupUiClassification(SETUP_UI_WORKING, reason="deterministic_flow")


def _awaiting_human(lowered: str) -> SetupUiClassification | None:
    if any(marker in lowered for marker in AWAITING_PROMPT_MARKERS):
        label = _first_button(lowered, AWAITING_BUTTONS) or "Add"
        return SetupUiClassification(
            SETUP_UI_AWAITING,
            prompt=_prompt_for(label),
            human_click_label=label,
            reason="confirm_dialog",
        )
    if "select item" in lowered or "pinch or spread" in lowered:
        label = _first_button(lowered, GALLERY_DONE_MARKERS) or "Done"
        return SetupUiClassification(
            SETUP_UI_AWAITING,
            prompt=_prompt_for(label),
            human_click_label=label,
            reason="gallery_confirm",
        )
    if _sim_manager_handoff(lowered):
        return SetupUiClassification(
            SETUP_UI_AWAITING,
            prompt=SIM_MANAGER_HANDOFF_PROMPT,
            human_click_label="Add eSIM",
            reason="sim_manager_handoff",
        )
    return None


def _sim_manager_handoff(lowered: str) -> bool:
    if "can't add" in lowered:
        return False
    if any(marker in lowered for marker in AWAITING_PROMPT_MARKERS):
        return False
    if "sim manager" in lowered:
        return True
    return any(marker in lowered for marker in SIM_MANAGER_MARKERS)


def _first_button(lowered: str, labels: frozenset[str] | tuple[str, ...]) -> str | None:
    for label in labels:
        if f">{label}<" in lowered or f'text="{label}"' in lowered or f"text='{label}'" in lowered:
            return label.title() if label != "ok" else "OK"
        # uiautomator: text="Add"
        needle = f'text="{label}"'
        if needle in lowered:
            return "OK" if label == "ok" else label.title()
    return None


def _prompt_for(label: str) -> str:
    clean = str(label or "Add").strip() or "Add"
    return f"Please tap '{clean}' on the screen below"


def public_setup_ui_body(classification: SetupUiClassification) -> dict[str, str]:
    body = {"setup_ui_state": classification.state}
    if classification.state == SETUP_UI_AWAITING and classification.prompt:
        body["setup_ui_prompt"] = classification.prompt
    if classification.human_click_label:
        body["setup_human_click"] = classification.human_click_label
    return body
