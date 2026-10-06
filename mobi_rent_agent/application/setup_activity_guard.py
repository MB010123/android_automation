"""Narrow setup-state whitelist for eSIM + VoidFix customer sessions.

Fail closed when the foreground activity cannot be identified. Do not
whitelist all of Android Settings.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

PHASE_ESIM = "esim"
PHASE_VOIDFIX = "voidfix"
KNOWN_PHASES = frozenset({PHASE_ESIM, PHASE_VOIDFIX})

# Public Settings intents only. Never EuiccManager / silent provision.
RECOVER_INTENT_ESIM = "android.settings.MANAGE_ALL_SIM_PROFILES_SETTINGS"
RECOVER_INTENT_VOIDFIX = "android.settings.MANAGE_DEFAULT_APPS_SETTINGS"

_ESIM_PACKAGES = frozenset(
    {
        "com.google.android.euicc",
        "com.android.euicc",
        "com.google.android.carriersetup",
    }
)
_ROLE_PACKAGES = frozenset(
    {
        "com.android.permissioncontroller",
        "com.google.android.permissioncontroller",
    }
)
_LAUNCHERS = (
    "launcher",
    "nexuslauncher",
    "lawnchair",
    "recents",
    "notification",
)
_ESIM_ACTIVITY_MARKERS = (
    "esim",
    "euicc",
    "simsettings",
    "simsetting",
    "mobilenetwork",
    "networkdashboard",
    "telephony",
    "embeddedsubscription",
    "managesim",
    "addesim",
    "provisionembedded",
)
_VOIDFIX_ACTIVITY_MARKERS = (
    "requestrole",
    "defaultapp",
    "defaultsms",
    "smsapplication",
    "managedefault",
    "role.ui",
)
_SETTINGS_GENERIC = (
    "settings$settingsactivity",
    "settings.SettingsActivity",
    "/.Settings",
    "SubSettings",
    "homepage",
    "searchactivity",
)


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    activity: str | None
    phase: str
    recover_intent: str | None
    reason: str


def parse_foreground_activity(dumpsys_text: str) -> str | None:
    """Best-effort parse of `dumpsys activity activities` / window focus."""
    text = dumpsys_text or ""
    for pattern in (
        r"mResumedActivity:.*? ([A-Za-z0-9._]+)/([A-Za-z0-9._$]+)",
        r"topResumedActivity=ActivityRecord\{[^}]* ([A-Za-z0-9._]+)/([A-Za-z0-9._$]+)",
        r"mCurrentFocus=Window\{[^}]* ([A-Za-z0-9._]+)/([A-Za-z0-9._$]+)",
        r"mFocusedApp=.*? ([A-Za-z0-9._]+)/([A-Za-z0-9._$]+)",
    ):
        match = re.search(pattern, text)
        if match:
            return f"{match.group(1)}/{match.group(2)}"
    return None


def parse_sms_role_holders(role_dump: str) -> tuple[str, ...]:
    """Package names holding android.app.role.SMS, if dumpsys exposes them."""
    text = role_dump or ""
    index = text.find("android.app.role.SMS")
    if index < 0:
        return ()
    rest = text[index:]
    next_role = re.search(r"\nRole:", rest[1:])
    block = rest[: next_role.start() + 1] if next_role else rest
    holders: list[str] = []
    for match in re.finditer(r"([a-zA-Z][A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+)", block):
        package = match.group(1)
        if package == "android.app.role.SMS":
            continue
        if package.startswith("android."):
            continue
        holders.append(package)
    return tuple(dict.fromkeys(holders))


def decide_setup_guard(
    *,
    phase: str,
    activity: str | None,
    voidfix_package: str | None = None,
) -> GuardDecision:
    """Allow only the current setup phase's screens. Unknown → not allowed."""
    current = str(phase or "").strip().lower()
    if current not in KNOWN_PHASES:
        return GuardDecision(False, activity, current or "unknown", None, "unknown_phase")
    recover = RECOVER_INTENT_ESIM if current == PHASE_ESIM else RECOVER_INTENT_VOIDFIX
    if not activity:
        return GuardDecision(False, None, current, recover, "activity_unknown")
    component = activity.strip()
    if "/" not in component:
        return GuardDecision(False, component, current, recover, "activity_unknown")
    package, _, cls = component.partition("/")
    lowered = f"{package}/{cls}".lower()
    if any(marker in lowered for marker in _LAUNCHERS):
        return GuardDecision(False, component, current, recover, "left_setup")
    if current == PHASE_ESIM:
        if _esim_allowed(package, lowered):
            return GuardDecision(True, component, current, None, "ok")
        return GuardDecision(False, component, current, recover, "left_setup")
    expected = str(voidfix_package or "").strip()
    if expected and package == expected:
        return GuardDecision(True, component, current, None, "ok")
    if _voidfix_allowed(package, lowered, expected):
        return GuardDecision(True, component, current, None, "ok")
    return GuardDecision(False, component, current, recover, "left_setup")


def _esim_allowed(package: str, lowered: str) -> bool:
    if package in _ESIM_PACKAGES:
        return True
    if package in {"com.android.settings", "com.android.phone"}:
        if any(generic.lower() in lowered for generic in _SETTINGS_GENERIC) and not any(
            marker in lowered for marker in _ESIM_ACTIVITY_MARKERS
        ):
            return False
        return any(marker in lowered for marker in _ESIM_ACTIVITY_MARKERS)
    if package in _ROLE_PACKAGES:
        return False
    return False


def _voidfix_allowed(package: str, lowered: str, expected_package: str) -> bool:
    if package in _ROLE_PACKAGES and any(marker in lowered for marker in _VOIDFIX_ACTIVITY_MARKERS):
        return True
    if package == "com.android.settings" and any(marker in lowered for marker in _VOIDFIX_ACTIVITY_MARKERS):
        return True
    if expected_package and package == expected_package:
        return True
    return False
