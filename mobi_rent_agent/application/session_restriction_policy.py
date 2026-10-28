"""Targeted customer-rental restrictions. Not the old setup-mode kiosk.

Renters may use Phone, SMS, the Home launcher, and eSIM/QR picker screens.
Chrome, Camera, Play Store, and other apps are kicked Home. Notification
shade and Quick Settings are blocked. Settings allows Network / SIMs /
Wi-Fi / About phone; Sound, Security, Accounts, and Reset are kicked.
GADS streaming / MediaProjection / IME stay allowed. Never restart GADS,
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
        "com.sec.android.app.camera",
        "com.samsung.android.app.camera",
        "com.sec.android.camera",
    }
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
    "soundsettings",
    "soundsandvibration",
    "soundandvibration",
    "soundmode",
    "volumesettings",
    "zenmode",
)
_SOUND_SETTINGS_MARKERS = (
    "soundsettings",
    "secsoundsettings",
    "soundsandvibration",
    "soundandvibration",
    "soundmode",
    "volumesettings",
    "secvolumesettings",
    "soundsettingsactivity",
    "zenmodesettings",
    "volumelimiter",
    "vibrationintensity",
    "vibrationpattern",
)
_SOUND_DUMP_ALWAYS_MARKERS = (
    "secsoundsettings",
    "soundsettings",
    "secvolumesettings",
    "volumelimiter",
    "vibrationintensity",
    "vibrationpattern",
    "zenmodesettings",
)

# On-device 1s guard allowlist. GADS stream package is infrastructure, not a renter app.
RENTER_LAUNCHER_PACKAGES = frozenset(
    {
        "com.google.android.apps.nexuslauncher",
        "com.sec.android.app.launcher",
        "com.android.launcher3",
        "com.samsung.android.launcher",
    }
)
RENTER_PHONE_PACKAGES = frozenset(
    {
        "com.google.android.dialer",
        "com.samsung.android.dialer",
        "com.samsung.android.incallui",
        "com.android.incallui",
        "com.android.server.telecom",
        "com.android.phone",
    }
)
RENTER_SMS_PACKAGES = frozenset(
    {
        "com.google.android.apps.messaging",
        "com.samsung.android.messaging",
        "com.android.mms",
    }
)
RENTER_ESIM_PACKAGES = frozenset(
    {
        "com.google.android.euicc",
        "com.android.euicc",
        "com.samsung.android.euicc",
        "com.samsung.android.app.telephonyui",
        "com.google.android.carriersetup",
        "com.google.android.providers.media.module",
        "com.android.providers.media.module",
        "com.android.documentsui",
        "com.google.android.documentsui",
        "com.google.android.photospicker",
    }
)
RENTER_IME_PACKAGES = frozenset(
    {
        "com.google.android.inputmethod.latin",
        "com.samsung.android.honeyboard",
        "com.sec.android.inputmethod",
        "com.google.android.tts",
    }
)
RENTER_SYSTEM_PACKAGES = frozenset(
    {
        "android",
        "com.android.systemui",
        "com.google.android.permissioncontroller",
        "com.android.permissioncontroller",
        "com.android.intentresolver",
        "com.android.bluetooth",
        "com.google.android.gms",
        GADS_SETTINGS_PACKAGE,
    }
)
RENTER_SETTINGS_PACKAGE = "com.android.settings"
PICKER_ONLY_PACKAGES = frozenset(
    {
        "com.samsung.android.scloud",
        "com.sec.android.app.myfiles",
        "com.sec.android.gallery3d",
        "com.samsung.android.gallery3d",
        "com.google.android.apps.photos",
    }
)
PICKER_OR_ESIM_MARKERS = (
    "photopicker",
    "picker",
    "documentsui",
    "captureactivity",
    "mediapicker",
    "getcontent",
    "euicc",
    "esim",
    "simcardmgr",
    "simcardmanager",
    "telephonyui",
    "managesim",
)
NEVER_FORCE_STOP_PACKAGES = frozenset(
    {
        "",
        "android",
        "com.android.systemui",
        GADS_SETTINGS_PACKAGE,
    }
)
RENTER_ALLOWED_PACKAGES = (
    RENTER_LAUNCHER_PACKAGES
    | RENTER_PHONE_PACKAGES
    | RENTER_SMS_PACKAGES
    | RENTER_ESIM_PACKAGES
    | RENTER_IME_PACKAGES
    | RENTER_SYSTEM_PACKAGES
    | {RENTER_SETTINGS_PACKAGE}
)
# Consumer apps to hide from the launcher. Never gallery/photos/documentsui (QR picker).
HIDDEN_USER_PACKAGES = (
    "com.android.chrome",
    "com.android.vending",
    "com.sec.android.app.samsungapps",
    "com.sec.android.app.sbrowser",
    "com.samsung.android.app.sbrowser",
    "com.google.android.youtube",
    "com.google.android.apps.youtube.music",
    "com.google.android.gm",
    "com.google.android.apps.maps",
    "com.google.android.calculator",
    "com.sec.android.app.popupcalculator",
    "com.google.android.calendar",
    "com.samsung.android.calendar",
    "com.google.android.deskclock",
    "com.sec.android.app.clockpackage",
    "com.google.android.apps.docs",
    "com.google.android.apps.wellbeing",
    "com.samsung.android.forest",
    "com.netflix.mediaclient",
    "com.netflix.partner.activation",
    "com.samsung.android.app.spage",
    "com.sec.android.app.kidshome",
)
# Class / fragment / title tokens. Avoid generic words like "application".
SENSITIVE_SETTINGS_DUMP_MARKERS = (
    "secsoundsettings",
    "soundsettings",
    "soundsandvibration",
    "volumesettings",
    "secvolume",
    "vibrationintensity",
    "vibrationpattern",
    "zenmode",
    "soundmode",
    "notificationsettings",
    "configurenotification",
    "appnotificationsettings",
    "notificationaccess",
    "statusbarsettings",
    "lockscreennotification",
    "securitysettings",
    "securityadvancedsettings",
    "biometricsandsecurity",
    "lockscreenmenu",
    "chooselockgeneric",
    "chooselockpassword",
    "chooselockpattern",
    "screenlock",
    "fingerprintsettings",
    "facesettings",
    "smartlock",
    "encryptionandcredential",
    "othersecuritysettings",
    "deviceadminsettings",
    "privacysettings",
    "privacydashboard",
    "resetdashboardfragment",
    "resetsettings",
    "secresetsettings",
    "generalmanagement",
    "masterclear",
    "factoryreset",
    "resetnetwork",
    "eraseesim",
    "backupsettings",
    "backupandrestore",
    "googlebackup",
    "systemdashboardfragment",
    "accountdashboardfragment",
    "accountssettings",
    "addaccountsettings",
    "manageaccounts",
    "usersettings",
    "multipleusers",
    "samsungaccount",
    "cloudaccountsettings",
    "googlesettings",
    "developmentsettings",
    "developeroptions",
    "usbsettings",
    "wirelessdebugging",
    "manageapplications",
    "appinfodashboard",
    "applicationsettings",
    "installedappdetails",
    "displaysettings",
    "secdisplaysettings",
    "batterysettings",
    "devicecare",
    "storagesettings",
    "accessibilitysettings",
    "wellbeing",
    "safetycenter",
    "safetycenteractivity",
)
SENSITIVE_SETTINGS_TITLE_MARKERS = (
    'text="soundsandvibration"',
    'text="security"',
    'text="notifications"',
    'text="accounts"',
    'text="backup"',
    'text="reset"',
    'text="generalmanagement"',
    'text="developeroptions"',
    'text="biometrics"',
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
        "com.android.chrome",
        "com.android.vending",
        "com.google.android.youtube",
        "com.google.android.gm",
        "com.google.android.apps.maps",
        "com.google.android.GoogleCamera",
        "com.sec.android.app.camera",
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


def _collapsed_sound_blob(text: str | None) -> str:
    return "".join(str(text or "").lower().split())


def is_sound_settings_activity(activity: str | None) -> bool:
    """True for Pixel/Samsung Sound & vibration / ringer screens.

    Matches component names and dump snippets. Samsung One UI often shows
    ``Settings$SecSoundSettingsActivity``, ``SubSettings`` plus a
    ``SecSoundSettings`` fragment, or the title ``Sounds and vibration``.
    """
    collapsed = _collapsed_sound_blob(activity)
    if not collapsed:
        return False
    return any(marker in collapsed for marker in _SOUND_SETTINGS_MARKERS)


def sound_settings_dump_matches(dump: str | None) -> bool:
    """True when window/activity dumps show a Sound & vibration screen."""
    text = str(dump or "")
    collapsed = _collapsed_sound_blob(text)
    if not collapsed:
        return False
    if any(marker in collapsed for marker in _SOUND_DUMP_ALWAYS_MARKERS):
        return True
    settings_top = "com.android.settings" in text.lower()
    if settings_top and ("soundmode" in collapsed or "soundsandvibration" in collapsed):
        return True
    return False


def foreground_package_from_dump(dump: str | None) -> str:
    """Package from mCurrentFocus / mFocusedApp / mResumedActivity lines."""
    for line in str(dump or "").splitlines():
        low = line.lower()
        if "mcurrentfocus" in low or "mfocusedapp" in low or "mresumedactivity" in low:
            pkg = package_from_focus_line(line)
            if pkg:
                return pkg
    return ""


def package_from_focus_line(line: str) -> str:
    lowered = str(line or "").lower()
    token = " u0 "
    idx = lowered.find(token)
    if idx < 0:
        token = " u10 "
        idx = lowered.find(token)
    if idx < 0:
        return ""
    rest = str(line)[idx + len(token) :]
    rest = rest.replace("}", " ").replace("{", " ")
    first = rest.split("/", 1)[0].strip().split()
    if not first:
        return ""
    pkg = first[0].strip()
    if pkg.lower() in {"null", "none"}:
        return ""
    return pkg


def package_from_activity(activity: str | None) -> str:
    text = str(activity or "").strip()
    if not text:
        return ""
    if "/" in text:
        return text.split("/", 1)[0].strip()
    return package_from_focus_line(text)


def is_picker_or_esim_context(dump: str | None) -> bool:
    collapsed = _collapsed_sound_blob(dump)
    if not collapsed:
        return False
    return any(marker in collapsed for marker in PICKER_OR_ESIM_MARKERS)


def permission_controller_is_security(dump: str | None) -> bool:
    collapsed = _collapsed_sound_blob(dump)
    return any(
        marker in collapsed
        for marker in (
            "securitysettings",
            "biometrics",
            "chooselock",
            "fingerprint",
            "facesettings",
            "privacysettings",
            "safetycenter",
        )
    )


def sensitive_settings_dump_matches(dump: str | None) -> bool:
    """True for Sound, Security, Accounts, Reset, and other blocked Settings pages."""
    if sound_settings_dump_matches(dump):
        return True
    collapsed = _collapsed_sound_blob(dump)
    if not collapsed:
        return False
    if any(marker in collapsed for marker in SENSITIVE_SETTINGS_DUMP_MARKERS):
        return True
    return any(marker in collapsed for marker in SENSITIVE_SETTINGS_TITLE_MARKERS)


def renter_should_kick_foreground(
    package: str | None,
    *,
    dump: str | None = None,
    activity: str | None = None,
) -> bool:
    """True when the on-device guard must send Home and stop this package."""
    pkg = str(package or package_from_activity(activity) or "").strip()
    if not pkg or pkg in NEVER_FORCE_STOP_PACKAGES or pkg in RENTER_IME_PACKAGES:
        return False
    blob = dump if dump else activity
    if pkg in PICKER_ONLY_PACKAGES:
        return not is_picker_or_esim_context(blob)
    if pkg == RENTER_SETTINGS_PACKAGE:
        return sensitive_settings_dump_matches(blob) or is_sound_settings_activity(activity)
    if pkg in {"com.google.android.permissioncontroller", "com.android.permissioncontroller"}:
        return permission_controller_is_security(blob)
    if pkg in RENTER_ALLOWED_PACKAGES:
        return False
    return True


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


def classify_foreground_activity(
    activity: str | None,
    *,
    voidfix_package: str | None = None,
    previous_kind: str | None = None,
) -> ActivityClass:
    _ = previous_kind
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
