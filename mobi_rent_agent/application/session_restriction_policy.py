"""Targeted customer-rental restrictions. Not the old setup-mode kiosk.

Home, Recents, rotate, and normal apps stay allowed. Notification shade and
Quick Settings are blocked (semantic actions and top-edge pull-downs). If
shade/QS is already visible, send Home. Settings opens the allowlisted Add
eSIM entry point. Back from the eSIM root goes Home. VoidFix, GADS Settings,
Companion UI, Camera (from the launcher/app menu), and other admin surfaces
are sent Home. Camera stays allowed when the eSIM/SIM flow invoked it for
QR scan or Scan from image. GADS streaming / MediaProjection / IME
components stay allowed. Enforcement is session-layer only: GADS home or
Farm ``am start`` of ``android.settings.MANAGE_ALL_SIM_PROFILES_SETTINGS``.
Never restart GADS, never lock-task, never recover-loop when the customer
is on Home.
"""
from __future__ import annotations

from dataclasses import dataclass

from application.setup_activity_guard import RECOVER_INTENT_ESIM

DEST_ADD_ESIM = "add_esim"
DEST_HOME = "home"

KIND_LAUNCHER = "launcher"
KIND_ESIM_ROOT = "esim_root"
KIND_ESIM_NESTED = "esim_nested"
KIND_SETTINGS = "settings"
KIND_SHADE = "shade"
KIND_BLOCKED_APP = "blocked_app"
KIND_OTHER = "other"
KIND_UNKNOWN = "unknown"

REASON_SETTINGS_REDIRECT = "settings_redirected"
REASON_SENSITIVE_BLOCKED = "sensitive_app_blocked"
REASON_SHADE_BLOCKED = "shade_blocked"
REASON_ESIM_BACK_HOME = "esim_back_to_home"
REASON_QR_NAVIGATE = "qr_navigated_to_add_esim"

SHADE_ACTIONS = frozenset({"notification_shade", "quick_settings"})
SHADE_PULL_MAX_START_Y = 120
SHADE_PULL_MIN_DISTANCE = 40

# Observed on Pixel 6 Slot 11 (sms role dump) plus the test fixture package.
KNOWN_VOIDFIX_PACKAGES = frozenset(
    {
        "org.voidfix.smsgateway",
        "com.voidfix.app",
    }
)

# Exact identities already used by this system. Do not invent aliases.
# GADS provider/devices/android.go getStreamServicePackageName / IME / cleanup.
GADS_SETTINGS_PACKAGE = "com.gads.settings"
# android_companion AndroidManifest package.
COMPANION_PACKAGE = "com.mobirent.companion"
# Pixel 6 Camera plus AOSP Camera packages. Do not invent vendor aliases.
CAMERA_PACKAGES = frozenset(
    {
        "com.google.android.GoogleCamera",
        "com.android.camera2",
        "com.android.camera",
    }
)
_ESIM_CAMERA_MARKERS = (
    "qrcode",
    "barcode",
    "scanqr",
    "qrscan",
    "qrcodescanner",
    "scanfromimage",
)

# Components hosted in com.gads.settings that are required for remote access.
# From GADS provider/devices/android.go — not the operator Settings UI.
_GADS_REMOTE_ACCESS_MARKERS = (
    "mjpegscreencapture",
    "audioprojection",
    "audioservice",
    "remotekeyboardime",
    "remotecontrolserver",
    "h264server",
)

# Existing Farm allowlisted intent. Do not invent a new component/package.
ADD_ESIM_INTENT = RECOVER_INTENT_ESIM

_LAUNCHER_MARKERS = (
    "launcher",
    "nexuslauncher",
    "lawnchair",
    "quickstep",
)
_ESIM_NESTED_PACKAGES = frozenset(
    {
        "com.google.android.euicc",
        "com.android.euicc",
        "com.google.android.carriersetup",
        "com.google.android.tsmclient",
        "com.android.tsmclient",
        # Samsung One UI eSIM / SIM Manager / Gallery / picker. Taps on these
        # must not be rewritten to the AOSP < SIMs fragment.
        "com.samsung.android.app.telephonyui",
        "com.samsung.android.euicc",
        "com.sec.android.gallery3d",
        "com.samsung.android.gallery3d",
        "com.google.android.documentsui",
        "com.android.documentsui",
        "com.samsung.android.biometrics.app.setting",
        "com.google.android.photospicker",
    }
)
_ESIM_NESTED_MARKERS = (
    "euicc",
    "esimprovision",
    "provisionembedded",
    "embeddedsubscription",
    "addesim",
    "lpa",
    "tsm",
    "simcardmgr",
    "spaactivity",
    "settings.spa",
    "/.spa.",
    "networkdashboard",
    "simcardmanager",
    "captureactivity",
)
_ESIM_ROOT_MARKERS = (
    "mobilenetwork",
    "mobilenetworklist",
    "simsettings",
    "simsetting",
    "managesim",
    "managesims",
    "allsimprofiles",
    "simonboarding",
    "simonboardingactivity",
    "simcardmgr",
    "simcardmanager",
)
_SETTINGS_GENERIC_MARKERS = (
    "settings$settingsactivity",
    "settings.SettingsActivity",
    "/.settings",
    "/.settings$",
    "subsettings",
    "homepage",
    "deeplinkhomepage",
    "searchactivity",
)
_SENSITIVE_SETTINGS_MARKERS = (
    "security",
    "account",
    "development",
    "developer",
    "application",
    "appdashboard",
    "manageappexternalsources",
    "unknownsource",
    "password",
    "biometrics",
    "encryption",
    "resetnetwork",
    "factoryreset",
    "systemdashboard",
    "userandaccount",
    "deviceadmin",
    "wifi",
    "bluetooth",
    "connecteddevice",
    "privacydashboard",
    "location",
    "notification",
)
_BLOCKED_APP_PACKAGES = frozenset(
    {
        "com.android.packageinstaller",
        "com.google.android.packageinstaller",
        "com.android.vpndialogs",
        "com.android.certinstaller",
        "com.android.settings.intelligence",
        "com.samsung.android.game.gamehome",
        "com.samsung.android.game.gametools",
        "com.samsung.android.game.gos",
        "com.enhance.gameservice",
    }
)
_BLOCKED_APP_MARKERS = (
    "packageinstaller",
    "installstart",
    "unknownsource",
    "deviceadmin",
    "developmentsettings",
)
_SHADE_PACKAGES = frozenset({"com.android.systemui"})
_SHADE_MARKERS = (
    "notificationshade",
    "notificationpanel",
    "quicksettings",
    "qspanel",
    ".shade.",
    "/shade/",
    "expandnotifications",
    "expandsettings",
)
_DIALOG_MARKERS = (
    "dialog",
    "alertdialog",
    "confirm",
    "chooser",
    "resolveractivity",
)

@dataclass(frozen=True)
class ActivityClass:
    kind: str
    activity: str | None
    reason: str


@dataclass(frozen=True)
class RestrictionDecision:
    destination: str | None
    reason: str
    rewrite_action: str | None = None
    launch_esim: bool = False
    send_home: bool = False
    escape_settings: bool = False
    allow_original: bool = True


def voidfix_packages(configured: str | None = None) -> frozenset[str]:
    packages = set(KNOWN_VOIDFIX_PACKAGES)
    extra = str(configured or "").strip()
    if extra:
        packages.add(extra)
    return frozenset(packages)


def operator_ui_packages() -> frozenset[str]:
    """Customer-blocked operator UIs. Same Home policy as VoidFix."""
    return frozenset({GADS_SETTINGS_PACKAGE, COMPANION_PACKAGE})


def _is_gads_remote_access(package: str, lowered: str) -> bool:
    return package == GADS_SETTINGS_PACKAGE and any(
        marker in lowered for marker in _GADS_REMOTE_ACCESS_MARKERS
    )


def is_shade_pull_swipe(
    *,
    y: int | None,
    y2: int | None,
    x: int | None = None,
    x2: int | None = None,
) -> bool:
    """True for a downward pull that starts at the status-bar edge."""
    if y is None or y2 is None:
        return False
    if y > SHADE_PULL_MAX_START_Y:
        return False
    dy = int(y2) - int(y)
    if dy < SHADE_PULL_MIN_DISTANCE:
        return False
    start_x = 0 if x is None else int(x)
    end_x = start_x if x2 is None else int(x2)
    return dy >= abs(end_x - start_x)


def _is_esim_invoked_camera(lowered: str, previous_kind: str | None) -> bool:
    if previous_kind in {KIND_ESIM_ROOT, KIND_ESIM_NESTED}:
        return True
    return any(marker in lowered for marker in _ESIM_CAMERA_MARKERS)


def classify_foreground_activity(
    activity: str | None,
    *,
    voidfix_package: str | None = None,
    previous_kind: str | None = None,
) -> ActivityClass:
    if not activity or "/" not in str(activity):
        return ActivityClass(KIND_UNKNOWN, activity, "activity_unknown")
    component = activity.strip()
    package, _, cls = component.partition("/")
    lowered = f"{package}/{cls}".lower()
    expected = voidfix_packages(voidfix_package)
    if package in expected or "voidfix" in package.lower():
        return ActivityClass(KIND_BLOCKED_APP, component, "voidfix")
    if _is_gads_remote_access(package, lowered):
        return ActivityClass(KIND_OTHER, component, "gads_remote_access")
    if package == GADS_SETTINGS_PACKAGE:
        return ActivityClass(KIND_BLOCKED_APP, component, "gads_settings")
    if package == COMPANION_PACKAGE:
        return ActivityClass(KIND_BLOCKED_APP, component, "companion")
    if package in {
        "com.samsung.android.game.gamehome",
        "com.samsung.android.game.gametools",
        "com.samsung.android.game.gos",
        "com.enhance.gameservice",
    }:
        return ActivityClass(KIND_BLOCKED_APP, component, "samsung_game")
    if package in CAMERA_PACKAGES:
        if _is_esim_invoked_camera(lowered, previous_kind):
            return ActivityClass(KIND_ESIM_NESTED, component, "esim_camera")
        return ActivityClass(KIND_BLOCKED_APP, component, "camera")
    if package in _SHADE_PACKAGES and any(marker in lowered for marker in _SHADE_MARKERS):
        return ActivityClass(KIND_SHADE, component, "shade")
    if any(marker in lowered for marker in _LAUNCHER_MARKERS):
        return ActivityClass(KIND_LAUNCHER, component, "launcher")
    if package in _ESIM_NESTED_PACKAGES or any(marker in lowered for marker in _ESIM_NESTED_MARKERS):
        return ActivityClass(KIND_ESIM_NESTED, component, "esim_nested")
    if package in {"com.android.settings", "com.android.phone"}:
        if any(marker in lowered for marker in _ESIM_ROOT_MARKERS):
            if any(marker in lowered for marker in _DIALOG_MARKERS):
                return ActivityClass(KIND_ESIM_NESTED, component, "esim_dialog")
            return ActivityClass(KIND_ESIM_ROOT, component, "esim_root")
        if any(marker in lowered for marker in _SENSITIVE_SETTINGS_MARKERS) or any(
            marker.lower() in lowered for marker in _SETTINGS_GENERIC_MARKERS
        ):
            return ActivityClass(KIND_SETTINGS, component, "settings")
        # Unknown Settings activity: treat as Settings, not a free-for-all.
        return ActivityClass(KIND_SETTINGS, component, "settings")
    if package in _BLOCKED_APP_PACKAGES or any(marker in lowered for marker in _BLOCKED_APP_MARKERS):
        return ActivityClass(KIND_BLOCKED_APP, component, "admin_app")
    return ActivityClass(KIND_OTHER, component, "other")


def decide_restriction(
    *,
    action: str,
    activity: str | None,
    voidfix_package: str | None = None,
    inspect_failed: bool = False,
    previous_kind: str | None = None,
    x: int | None = None,
    y: int | None = None,
    x2: int | None = None,
    y2: int | None = None,
) -> RestrictionDecision:
    """Decide a one-shot restriction for this control. Never a recover loop."""
    current = str(action or "").strip().lower()
    shade_pull = current == "swipe" and is_shade_pull_swipe(y=y, y2=y2, x=x, x2=x2)
    if inspect_failed:
        if current == "settings":
            return RestrictionDecision(
                DEST_ADD_ESIM,
                REASON_SETTINGS_REDIRECT,
                launch_esim=True,
                allow_original=False,
            )
        if current in SHADE_ACTIONS:
            return RestrictionDecision(
                DEST_HOME,
                REASON_SHADE_BLOCKED,
                rewrite_action="home",
                send_home=True,
                allow_original=False,
            )
        if shade_pull:
            return RestrictionDecision(None, REASON_SHADE_BLOCKED, allow_original=False)
        return RestrictionDecision(None, "inspect_unavailable")
    classified = classify_foreground_activity(
        activity, voidfix_package=voidfix_package, previous_kind=previous_kind
    )
    if current in SHADE_ACTIONS:
        return RestrictionDecision(
            DEST_HOME,
            REASON_SHADE_BLOCKED,
            rewrite_action="home",
            send_home=True,
            allow_original=False,
        )
    if shade_pull:
        return RestrictionDecision(None, REASON_SHADE_BLOCKED, allow_original=False)
    if current == "settings":
        return RestrictionDecision(
            DEST_ADD_ESIM,
            REASON_SETTINGS_REDIRECT,
            launch_esim=True,
            escape_settings=classified.kind == KIND_SETTINGS,
            allow_original=False,
        )
    if current == "back":
        if classified.kind in {KIND_ESIM_ROOT, KIND_SETTINGS}:
            reason = REASON_ESIM_BACK_HOME if classified.kind == KIND_ESIM_ROOT else REASON_SETTINGS_REDIRECT
            return RestrictionDecision(
                DEST_HOME,
                reason,
                rewrite_action="home",
                send_home=True,
                allow_original=False,
            )
        if classified.kind == KIND_BLOCKED_APP:
            return RestrictionDecision(
                DEST_HOME,
                REASON_SENSITIVE_BLOCKED,
                rewrite_action="home",
                send_home=True,
                allow_original=False,
            )
        if classified.kind == KIND_SHADE:
            return RestrictionDecision(
                DEST_HOME,
                REASON_SHADE_BLOCKED,
                rewrite_action="home",
                send_home=True,
                allow_original=False,
            )
        return RestrictionDecision(None, "allow_back", allow_original=True)
    if classified.kind == KIND_SETTINGS:
        return RestrictionDecision(
            DEST_ADD_ESIM,
            REASON_SETTINGS_REDIRECT,
            launch_esim=True,
            escape_settings=True,
            allow_original=True,
        )
    if classified.kind == KIND_BLOCKED_APP:
        return RestrictionDecision(
            DEST_HOME,
            REASON_SENSITIVE_BLOCKED,
            send_home=True,
            allow_original=True,
        )
    if classified.kind == KIND_SHADE:
        return RestrictionDecision(
            DEST_HOME,
            REASON_SHADE_BLOCKED,
            send_home=True,
            allow_original=True,
        )
    return RestrictionDecision(None, "allow")


def public_restriction_body(decision: RestrictionDecision) -> dict[str, str]:
    if decision.reason == REASON_SHADE_BLOCKED:
        body = {"restriction": REASON_SHADE_BLOCKED}
        if decision.destination:
            body["restricted_destination"] = decision.destination
        return body
    if not decision.destination:
        return {}
    return {
        "restricted_destination": decision.destination,
        "restriction": decision.reason,
    }


def add_esim_public_body(*, reason: str = REASON_SETTINGS_REDIRECT) -> dict[str, str]:
    """Customer-visible Add eSIM redirect. Never includes activity, serial, or intent."""
    return {
        "restricted_destination": DEST_ADD_ESIM,
        "restriction": reason,
    }
