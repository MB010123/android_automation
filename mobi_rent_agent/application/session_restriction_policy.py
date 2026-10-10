"""Targeted customer-rental restrictions. Not the old setup-mode kiosk.

Home, Recents, shade, Quick Settings, rotate, and normal apps stay allowed.
Settings opens the allowlisted Add eSIM entry point. Back from the eSIM root
goes Home. VoidFix and other admin surfaces are sent Home. Enforcement is
session-layer only: GADS home or Farm ``am start`` of
``android.settings.MANAGE_ALL_SIM_PROFILES_SETTINGS``. Never restart GADS,
never lock-task, never recover-loop when the customer is on Home.
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
KIND_BLOCKED_APP = "blocked_app"
KIND_OTHER = "other"
KIND_UNKNOWN = "unknown"

REASON_SETTINGS_REDIRECT = "settings_redirected"
REASON_SENSITIVE_BLOCKED = "sensitive_app_blocked"
REASON_ESIM_BACK_HOME = "esim_back_to_home"
REASON_QR_NAVIGATE = "qr_navigated_to_add_esim"

# Observed on Pixel 6 Slot 11 (sms role dump) plus the test fixture package.
KNOWN_VOIDFIX_PACKAGES = frozenset(
    {
        "org.voidfix.smsgateway",
        "com.voidfix.app",
    }
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
)
_ESIM_ROOT_MARKERS = (
    "mobilenetwork",
    "simsettings",
    "simsetting",
    "managesim",
    "managesims",
    "allsimprofiles",
)
_SETTINGS_GENERIC_MARKERS = (
    "settings$settingsactivity",
    "settings.SettingsActivity",
    "/.settings",
    "subsettings",
    "homepage",
    "searchactivity",
    "networkdashboard",
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
    }
)
_BLOCKED_APP_MARKERS = (
    "packageinstaller",
    "installstart",
    "unknownsource",
    "deviceadmin",
    "developmentsettings",
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
    allow_original: bool = True


def voidfix_packages(configured: str | None = None) -> frozenset[str]:
    packages = set(KNOWN_VOIDFIX_PACKAGES)
    extra = str(configured or "").strip()
    if extra:
        packages.add(extra)
    return frozenset(packages)


def classify_foreground_activity(
    activity: str | None,
    *,
    voidfix_package: str | None = None,
) -> ActivityClass:
    if not activity or "/" not in str(activity):
        return ActivityClass(KIND_UNKNOWN, activity, "activity_unknown")
    component = activity.strip()
    package, _, cls = component.partition("/")
    lowered = f"{package}/{cls}".lower()
    expected = voidfix_packages(voidfix_package)
    if package in expected or "voidfix" in package.lower():
        return ActivityClass(KIND_BLOCKED_APP, component, "voidfix")
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
) -> RestrictionDecision:
    """Decide a one-shot restriction for this control. Never a recover loop."""
    current = str(action or "").strip().lower()
    if inspect_failed:
        if current == "settings":
            return RestrictionDecision(
                DEST_ADD_ESIM,
                REASON_SETTINGS_REDIRECT,
                launch_esim=True,
                allow_original=False,
            )
        return RestrictionDecision(None, "inspect_unavailable")
    classified = classify_foreground_activity(activity, voidfix_package=voidfix_package)
    if current == "settings":
        return RestrictionDecision(
            DEST_ADD_ESIM,
            REASON_SETTINGS_REDIRECT,
            launch_esim=True,
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
        return RestrictionDecision(None, "allow_back", allow_original=True)
    if classified.kind == KIND_SETTINGS:
        return RestrictionDecision(
            DEST_ADD_ESIM,
            REASON_SETTINGS_REDIRECT,
            launch_esim=True,
            allow_original=True,
        )
    if classified.kind == KIND_BLOCKED_APP:
        return RestrictionDecision(
            DEST_HOME,
            REASON_SENSITIVE_BLOCKED,
            send_home=True,
            allow_original=True,
        )
    return RestrictionDecision(None, "allow")


def public_restriction_body(decision: RestrictionDecision) -> dict[str, str]:
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
