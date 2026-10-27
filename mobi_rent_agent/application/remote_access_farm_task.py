"""Farm Agent task: place a validated eSIM QR image on the phone.

Pixel renters get ``/sdcard/DCIM/Camera/``. Samsung S20 renters (Slot 21 /
``RFCNB056WEV`` / manufacturer Samsung) get ``/sdcard/Pictures/``. Every
renter QR is also copied to ``/sdcard/Pictures/`` and ``/sdcard/Download/``
and media-scanned. After the push the farm wakes the phone and opens the
OEM eSIM screen (Samsung SIM Manager / Wireless Settings, Pixel
``MANAGE_EMBEDDED_SUBSCRIPTIONS`` / Wireless Settings), then stop. Admin
payloads may keep DCIM or pick an allowlisted directory.

Explicitly NOT done here: no ``provision_esim``, no companion socket, no
EuiccManager, no LPA parsing, no WRITE_EMBEDDED_SUBSCRIPTIONS, no policy or
owner changes. The device only receives a picture file plus a public
Settings/SIM-manager intent.
"""
from __future__ import annotations

import base64
import binascii
import logging
import os
import re
import tempfile
from io import BytesIO
from typing import Any, Callable

import requests

from application.esim_setup_ui_state import (
    OEM_PIXEL,
    OEM_SAMSUNG,
    SETUP_UI_AWAITING,
    SETUP_UI_WORKING,
    handoff_prompt_for_oem,
)
from application.farm_task_types import FarmTaskRequest, FarmTaskResult
from application.samsung_one_ui import (
    package_from_component,
    samsung_network_args,
    samsung_sim_manager_args,
)
from infrastructure.adb_companion import AdbCommandError, AdbCommandRunner
from infrastructure.config import AgentConfig
from infrastructure.esim_qr_security import esim_fetch_url_is_public_https, esim_fetch_url_is_safe

logger = logging.getLogger("farm_agent.remote_access_place_qr")


def _poc_slot_gate(agent_config: AgentConfig | None, slot_id: int) -> FarmTaskResult | None:
    """Legacy Slot-1 POC allowlist.

    Unused by customer QR placement, in-app GADS, activation observation, or
    complete. Silent eSIM / companion ``provision_esim`` still uses
    ``PROVISIONING_ALLOWED_SLOT_IDS`` (default Slot 1), not this gate.
    """
    if agent_config is None:
        return FarmTaskResult(ok=False, http_status=503, error="agent_not_configured")
    if not getattr(agent_config, "remote_access_poc_enabled", False):
        return FarmTaskResult(ok=False, http_status=403, error="remote_access_poc_disabled")
    allowed = tuple(getattr(agent_config, "remote_access_poc_slot_ids", ()) or ())
    if slot_id not in allowed:
        return FarmTaskResult(
            ok=False,
            http_status=403,
            error="slot_not_allowlisted",
            message=f"slot {slot_id} is outside REMOTE_ACCESS_POC_SLOT_IDS {sorted(allowed)}",
        )
    return None


def _place_qr_config_gate(agent_config: AgentConfig | None) -> FarmTaskResult | None:
    """Customer remote-access Farm enable-flag only.

    Does not read ``remote_access_poc_slot_ids`` / ``REMOTE_ACCESS_POC_SLOT_IDS``.
    QR placement and read-only activation observation may run for any bay in
    ``slot_map.json``. Customer JWT → rental → bay binding is enforced on the VPS.
    """
    if agent_config is None:
        return FarmTaskResult(ok=False, http_status=503, error="agent_not_configured")
    if not bool(getattr(agent_config, "remote_access_poc_enabled", False)):
        return FarmTaskResult(ok=False, http_status=403, error="remote_access_poc_disabled")
    return None

DCIM_CAMERA_DIR = "/sdcard/DCIM/Camera"
DCIM_DIR = "/sdcard/DCIM"
PICTURES_DIR = "/sdcard/Pictures"
DOWNLOAD_DIR = "/sdcard/Download"
ALLOWED_QR_DIRS = frozenset({DCIM_CAMERA_DIR, PICTURES_DIR, DOWNLOAD_DIR})
SAMSUNG_S20_SERIAL = "RFCNB056WEV"
SAMSUNG_USER_QR_SLOT = 21
SAMSUNG_GALLERY_QR_PATHS = (
    f"{PICTURES_DIR}/esim_qr.png",
    f"{DOWNLOAD_DIR}/esim_qr.png",
    f"{DCIM_DIR}/esim_qr.png",
)
PLACED_ESIM_QR_PATHS = SAMSUNG_GALLERY_QR_PATHS
RENTER_GALLERY_QR_PATHS = (
    f"{PICTURES_DIR}/esim_qr.png",
    f"{DOWNLOAD_DIR}/esim_qr.png",
)
PIXEL_SETTINGS_INTENT = "android.settings.SETTINGS"
PIXEL_WIRELESS_INTENT = "android.settings.WIRELESS_SETTINGS"
PIXEL_MANAGE_EMBEDDED_INTENT = "android.telephony.euicc.action.MANAGE_EMBEDDED_SUBSCRIPTIONS"
PIXEL_ESIM_INTENTS = (
    PIXEL_MANAGE_EMBEDDED_INTENT,
    PIXEL_WIRELESS_INTENT,
    PIXEL_SETTINGS_INTENT,
)
PIXEL_SETTINGS_PACKAGE = "com.android.settings"
SAMSUNG_ESIM_FALLBACK_INTENT = "android.settings.WIRELESS_SETTINGS"
SAMSUNG_CAMERA_PACKAGES = (
    "com.sec.android.app.camera",
    "com.samsung.android.app.camera",
    "com.sec.android.camera",
)
PIXEL_CAMERA_PACKAGES = (
    "com.google.android.GoogleCamera",
    "com.android.camera2",
    "com.android.camera",
)
ALL_CAMERA_PACKAGES = SAMSUNG_CAMERA_PACKAGES + PIXEL_CAMERA_PACKAGES
LAUNCHER_PACKAGES = frozenset(
    {
        "com.sec.android.app.launcher",
        "com.google.android.apps.nexuslauncher",
        "com.android.launcher3",
        "com.samsung.android.launcher",
        "com.android.launcher",
    }
)
SAMSUNG_HANDOFF_PACKAGES = frozenset(
    {
        "com.samsung.android.app.telephonyui",
        "com.samsung.android.euicc",
    }
)
PIXEL_HANDOFF_TEXT = (
    "add sim",
    "set up an esim",
    "add esim",
    "download a sim",
)
_EMBEDDED_ID_RE = re.compile(r"(?:iccId|IccId|mIccId|cardId|mCardId)=([A-Za-z0-9]+)")
QR_PREFIX = "mobirent_esim_qr_"
QR_WALK_MARKER_PATH = f"{PICTURES_DIR}/.mobirent_qr_walk_done"
MAX_QR_IMAGE_BYTES = 5 * 1024 * 1024
_RENTAL_TOKEN_RE = re.compile(r"[^a-z0-9]")

Downloader = Callable[[str, float], bytes]


def _is_samsung_renter_device(slot_id: int, serial: str, manufacturer: str | None) -> bool:
    if int(slot_id) == SAMSUNG_USER_QR_SLOT:
        return True
    if str(serial or "").strip().upper() == SAMSUNG_S20_SERIAL:
        return True
    brand = str(manufacturer or "").strip().lower()
    return brand == "samsung" or brand.startswith("samsung")


def qr_placement_dir(
    *,
    slot_id: int,
    serial: str,
    payload: dict[str, Any] | None,
    manufacturer: str | None = None,
) -> str:
    """Directory for the QR file. Renters cannot override; admins may.

    Admin ``actor=admin`` keeps Pixel DCIM unless ``qr_dir`` is an allowlisted
    path. Renter Samsung devices always use Pictures.
    """
    body = payload if isinstance(payload, dict) else {}
    actor = str(body.get("actor") or "renter").strip().lower()
    requested = str(body.get("qr_dir") or "").strip().rstrip("/")
    if actor == "admin":
        if requested in ALLOWED_QR_DIRS:
            return requested
        return DCIM_CAMERA_DIR
    if _is_samsung_renter_device(int(slot_id), serial, manufacturer):
        return PICTURES_DIR
    return DCIM_CAMERA_DIR


def _media_scan(runner: AdbCommandRunner, serial: str, remote_path: str) -> bool:
    try:
        runner.run(
            serial,
            [
                "shell",
                "am",
                "broadcast",
                "-a",
                "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                "-d",
                f"file://{remote_path}",
            ],
        )
        return True
    except AdbCommandError:
        return False


def delete_placed_esim_qr(runner: AdbCommandRunner, serial: str) -> list[str]:
    """Remove Gallery aliases and hashed QR files so they cannot be re-picked."""
    removed: list[str] = []
    directories = (PICTURES_DIR, DOWNLOAD_DIR, DCIM_DIR, DCIM_CAMERA_DIR)
    targets = list(PLACED_ESIM_QR_PATHS)
    for directory in directories:
        try:
            listing = runner.run(serial, ["shell", "ls", directory]).stdout or ""
        except AdbCommandError:
            listing = ""
        for name in listing.split():
            if name.startswith(QR_PREFIX):
                targets.append(f"{directory}/{name}")
    seen: set[str] = set()
    for path in targets:
        if path in seen:
            continue
        seen.add(path)
        try:
            runner.run(serial, ["shell", "rm", "-f", path])
            _media_scan(runner, serial, path)
            removed.append(path)
        except AdbCommandError:
            continue
    return removed


def write_qr_walk_marker(runner: AdbCommandRunner, serial: str, token: str | None = None) -> None:
    marker = str(token or "1").strip() or "1"
    try:
        runner.run(serial, ["shell", f"echo {marker} > {QR_WALK_MARKER_PATH}"])
    except AdbCommandError:
        return


def read_qr_walk_marker(runner: AdbCommandRunner, serial: str) -> str | None:
    try:
        text = (runner.run(serial, ["shell", "cat", QR_WALK_MARKER_PATH]).stdout or "").strip()
    except AdbCommandError:
        return None
    return text or None


def copy_renter_gallery_aliases(
    runner: AdbCommandRunner, serial: str, source_path: str, *, oem: str
) -> list[str]:
    """Stable Gallery/Photos names so the renter QR is first in the picker."""
    destinations = SAMSUNG_GALLERY_QR_PATHS if oem == OEM_SAMSUNG else (
        *RENTER_GALLERY_QR_PATHS,
        f"{DCIM_DIR}/esim_qr.png",
    )
    copied: list[str] = []
    for dest in destinations:
        parent = dest.rsplit("/", 1)[0]
        try:
            runner.run(serial, ["shell", "mkdir", "-p", parent])
            if dest != source_path:
                runner.run(serial, ["shell", "cp", source_path, dest])
            _media_scan(runner, serial, dest)
            copied.append(dest)
        except AdbCommandError:
            continue
    return copied


def _manufacturer_from_adb(runner: AdbCommandRunner, serial: str) -> str:
    return _prop_from_adb(runner, serial, "ro.product.manufacturer")


def _prop_from_adb(runner: AdbCommandRunner, serial: str, name: str) -> str:
    try:
        return (runner.run(serial, ["shell", "getprop", name]).stdout or "").strip()
    except AdbCommandError:
        return ""


def detect_device_oem(runner: AdbCommandRunner, serial: str, slot_id: int) -> str:
    manufacturer = _prop_from_adb(runner, serial, "ro.product.manufacturer")
    brand = _prop_from_adb(runner, serial, "ro.product.brand")
    if _is_samsung_renter_device(int(slot_id), serial, manufacturer):
        return OEM_SAMSUNG
    if _is_samsung_renter_device(int(slot_id), serial, brand):
        return OEM_SAMSUNG
    blob = f"{manufacturer} {brand}".lower()
    if "samsung" in blob:
        return OEM_SAMSUNG
    return OEM_PIXEL


def wake_and_unlock_device(runner: AdbCommandRunner, serial: str) -> None:
    """Wake and dismiss the keyguard. Does not send KEYCODE_HOME."""
    try:
        runner.run(serial, ["shell", "input", "keyevent", "224"])
    except AdbCommandError:
        pass
    for args in (
        ["shell", "wm", "dismiss-keyguard"],
        ["shell", "input", "keyevent", "82"],
    ):
        try:
            runner.run(serial, args)
        except AdbCommandError:
            pass


def read_foreground_component(runner: AdbCommandRunner, serial: str) -> str:
    """Foreground activity from dumpsys window, then activity activities."""
    from application.setup_activity_guard import parse_foreground_activity

    for args in (
        ["shell", "dumpsys", "window"],
        ["shell", "dumpsys", "activity", "activities"],
    ):
        try:
            text = runner.run(serial, args).stdout or ""
        except AdbCommandError:
            continue
        hit = parse_foreground_activity(text)
        if hit:
            return hit
    return ""


def foreground_is_launcher(component: str | None) -> bool:
    pkg = package_from_component(component).strip().lower()
    if not pkg:
        return False
    if pkg in LAUNCHER_PACKAGES:
        return True
    return "launcher" in pkg


def pixel_settings_is_foreground(component: str | None) -> bool:
    return package_from_component(component).strip().lower() == PIXEL_SETTINGS_PACKAGE


def oem_setup_screen_is_open(component: str | None, oem: str) -> bool:
    """True when dumpsys shows the OEM SIM/eSIM UI, never the launcher."""
    if not component or foreground_is_launcher(component):
        return False
    pkg = package_from_component(component).strip().lower()
    if pkg == PIXEL_SETTINGS_PACKAGE:
        return True
    if oem == OEM_SAMSUNG:
        return pkg in SAMSUNG_HANDOFF_PACKAGES or "telephonyui" in pkg
    return pkg in {"com.google.android.euicc", "com.android.euicc"}


def esim_handoff_is_open(component: str, oem: str, ui_xml: str = "") -> bool:
    if oem_setup_screen_is_open(component, oem):
        return True
    if foreground_is_launcher(component):
        return False
    blob = f"{component} {ui_xml}".lower()
    if oem == OEM_SAMSUNG:
        return "add esim" in blob or "add mobile plan" in blob
    return any(marker in blob for marker in PIXEL_HANDOFF_TEXT)


def camera_packages_for_oem(oem: str) -> tuple[str, ...]:
    if oem == OEM_SAMSUNG:
        return SAMSUNG_CAMERA_PACKAGES
    return PIXEL_CAMERA_PACKAGES


def disable_native_camera_commands(oem: str) -> tuple[str, ...]:
    commands: list[str] = []
    for package in camera_packages_for_oem(oem):
        commands.append(f"pm disable-user --user 0 {package}")
        commands.append(f"am force-stop {package}")
    return tuple(commands)


def enable_native_camera_commands() -> tuple[str, ...]:
    return tuple(f"pm enable {package}" for package in ALL_CAMERA_PACKAGES)


def disable_native_cameras(runner: AdbCommandRunner, serial: str, oem: str) -> None:
    for command in disable_native_camera_commands(oem):
        _try_shell(runner, serial, ["shell", *command.split()])


def enable_native_cameras(runner: AdbCommandRunner, serial: str) -> None:
    for command in enable_native_camera_commands():
        _try_shell(runner, serial, ["shell", *command.split()])


def launch_esim_setup_screen(runner: AdbCommandRunner, serial: str, oem: str) -> str:
    """Open the OEM SIM/eSIM screen, then stop. No Gallery/Add eSIM taps.

    Uses public intents and dumpsys only. uiautomator dumps interrupt the
    GADS MediaProjection stream, so this path never dumps XML or taps
    Add eSIM / Scan QR / Gallery.
    """
    if oem == OEM_SAMSUNG:
        _try_shell(runner, serial, ["shell", *samsung_sim_manager_args()])
        if _esim_handoff_verified(runner, serial, oem):
            return "sim_manager"
        _try_shell(runner, serial, ["shell", "am", "start", "-a", SAMSUNG_ESIM_FALLBACK_INTENT])
        if _esim_handoff_verified(runner, serial, oem):
            return SAMSUNG_ESIM_FALLBACK_INTENT
        _try_shell(runner, serial, ["shell", *samsung_network_args()])
        if _esim_handoff_verified(runner, serial, oem):
            return "network_dashboard"
        return "sim_manager_unconfirmed"
    for intent in PIXEL_ESIM_INTENTS:
        _try_shell(runner, serial, ["shell", "am", "start", "-a", intent])
        if _esim_handoff_verified(runner, serial, oem):
            return intent
    return "pixel_sims_unconfirmed"


def _try_shell(runner: AdbCommandRunner, serial: str, arguments: list[str]) -> None:
    try:
        runner.run(serial, arguments)
    except AdbCommandError:
        pass


def _esim_handoff_verified(runner: AdbCommandRunner, serial: str, oem: str) -> bool:
    return oem_setup_screen_is_open(read_foreground_component(runner, serial), oem)


def esim_subscription_token(isub: str) -> str:
    ids = sorted(set(_EMBEDDED_ID_RE.findall(isub or "")))
    if ids:
        return "ids:" + ",".join(ids)
    from infrastructure.adb_four_layer import _subscription_bits

    present, default_data, empty = _subscription_bits(isub or "")
    return f"bits:{int(bool(present))}:{default_data}:{int(bool(empty))}"


def capture_esim_baseline(runner: AdbCommandRunner, serial: str) -> dict[str, Any]:
    try:
        isub = runner.run(serial, ["shell", "dumpsys", "isub"]).stdout or ""
    except AdbCommandError:
        isub = ""
    from infrastructure.adb_four_layer import _subscription_bits

    present, _default, _empty = _subscription_bits(isub)
    token = esim_subscription_token(isub)
    return {
        "esim_profile_present": bool(present),
        "esim_baseline_present": bool(present),
        "esim_baseline_token": token,
        "esim_current_token": token,
    }


def _default_downloader(url: str, timeout_seconds: float) -> bytes:
    response = requests.get(url, timeout=timeout_seconds, stream=True)
    response.raise_for_status()
    length = response.headers.get("Content-Length")
    if length and int(length) > MAX_QR_IMAGE_BYTES:
        raise ValueError("qr_image_too_large")
    data = bytearray()
    for chunk in response.iter_content(chunk_size=64 * 1024):
        data.extend(chunk)
        if len(data) > MAX_QR_IMAGE_BYTES:
            raise ValueError("qr_image_too_large")
    return bytes(data)


def _image_extension(payload: bytes) -> str | None:
    """Verify the bytes are a real raster image; return a file extension."""
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:  # pragma: no cover - PIL is a runtime dependency
        return "png" if payload[:8] == b"\x89PNG\r\n\x1a\n" else None
    try:
        with Image.open(BytesIO(payload)) as image:
            image.verify()
            fmt = (image.format or "").lower()
    except (UnidentifiedImageError, OSError, ValueError):
        return None
    if fmt in {"png", "jpeg", "webp"}:
        return "jpg" if fmt == "jpeg" else fmt
    return None


def _inline_image_from_payload(payload: dict[str, Any]) -> bytes | None:
    """Decode Farm payload image_base64. Never logs the bytes."""
    raw = payload.get("image_base64")
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise ValueError("qr_not_an_image")
    text = raw.strip()
    if text.startswith("data:") and "," in text:
        text = text.split(",", 1)[1]
    try:
        data = base64.b64decode(text, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("qr_not_an_image") from exc
    if not data:
        raise ValueError("qr_not_an_image")
    return data


def remote_qr_filename(rental_id: str, job_id: str, extension: str) -> str:
    rental_token = _RENTAL_TOKEN_RE.sub("", str(rental_id).lower())[:12] or "rental"
    job_token = _RENTAL_TOKEN_RE.sub("", str(job_id).lower())[:8] or "job"
    return f"mobirent_esim_qr_{rental_token}_{job_token}.{extension}"


def _parse_remote_byte_size(stdout: str) -> int | None:
    """Parse `wc -c` (`SIZE PATH`) or `stat -c %s` (`SIZE`) output."""
    token = (stdout or "").strip().split()
    if not token:
        return None
    try:
        value = int(token[0])
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value


def _read_remote_byte_size(runner: AdbCommandRunner, serial: str, remote_path: str) -> int | None:
    try:
        raw = runner.run(serial, ["shell", "wc", "-c", remote_path]).stdout
    except AdbCommandError:
        try:
            raw = runner.run(serial, ["shell", "stat", "-c", "%s", remote_path]).stdout
        except AdbCommandError:
            return None
    return _parse_remote_byte_size(raw)


def _place_details(
    *,
    ok: bool,
    job_id: str,
    serial: str | None = None,
    destination: str | None = None,
    downloaded_size: int | None = None,
    remote_size: int | None = None,
    placed: bool = False,
    error_code: str | None = None,
    message: str | None = None,
    media_scanned: bool | None = None,
    gallery_aliases: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "ok": ok,
        "job_id": job_id,
        "serial": serial,
        "destination": destination,
        "downloaded_size": downloaded_size,
        "remote_size": remote_size,
        "placed": placed,
        "error_code": error_code,
        "message": message,
    }
    if media_scanned is not None:
        body["media_scanned"] = media_scanned
    if gallery_aliases:
        body["gallery_aliases"] = list(gallery_aliases)
    if extra:
        body.update(extra)
    return body


def _place_result(
    *,
    ok: bool,
    http_status: int,
    job_id: str,
    error: str | None = None,
    message: str | None = None,
    serial: str | None = None,
    destination: str | None = None,
    downloaded_size: int | None = None,
    remote_size: int | None = None,
    placed: bool = False,
    media_scanned: bool | None = None,
    gallery_aliases: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> FarmTaskResult:
    details = _place_details(
        ok=ok,
        job_id=job_id,
        serial=serial,
        destination=destination,
        downloaded_size=downloaded_size,
        remote_size=remote_size,
        placed=placed,
        error_code=error,
        message=message,
        media_scanned=media_scanned,
        gallery_aliases=gallery_aliases,
        extra=extra,
    )
    return FarmTaskResult(
        ok=ok,
        http_status=http_status,
        error=error,
        message=message,
        details=details,
    )


def run_remote_access_place_qr(
    *,
    adb_path: str,
    slot_map: dict[int, str],
    request: FarmTaskRequest,
    agent_config: AgentConfig | None,
    command_runner: AdbCommandRunner | None = None,
    downloader: Downloader | None = None,
) -> FarmTaskResult:
    slot_id = int(request.farm_slot_id)
    # Enable-flag only. Slot mapping is slot_map.json, not the GADS POC allowlist.
    gated = _place_qr_config_gate(agent_config)
    if gated is not None:
        return _place_result(
            ok=False,
            http_status=gated.http_status,
            job_id=request.job_id,
            error=gated.error,
            message=gated.message,
        )
    assert agent_config is not None
    serial = str(slot_map.get(slot_id) or "").strip()
    if not serial:
        return _place_result(
            ok=False,
            http_status=404,
            job_id=request.job_id,
            error="slot_not_found",
            message="farm slot has no ADB serial in slot_map.json",
        )

    rental_id = str(request.payload.get("rental_id") or request.job_id)
    try:
        inline = _inline_image_from_payload(request.payload)
    except ValueError:
        return _place_result(
            ok=False,
            http_status=422,
            job_id=request.job_id,
            serial=serial,
            error="qr_not_an_image",
            message="QR payload is not a PNG, JPG, or WEBP image",
        )
    if inline is not None:
        payload = inline
    else:
        qr_url = str(request.payload.get("esim_qr_url") or "").strip()
        if not qr_url or not esim_fetch_url_is_public_https(qr_url):
            return _place_result(
                ok=False,
                http_status=422,
                job_id=request.job_id,
                serial=serial,
                error="invalid_assignment",
                message="esim_qr_url must be a public HTTPS URL",
            )
        prefixes = tuple(getattr(agent_config, "remote_access_qr_url_prefixes", ()) or ())
        if prefixes and not esim_fetch_url_is_safe(qr_url, allowed_url_prefixes=prefixes):
            return _place_result(
                ok=False,
                http_status=422,
                job_id=request.job_id,
                serial=serial,
                error="invalid_assignment",
                message="esim_qr_url must be a public HTTPS URL",
            )
        fetch = downloader or _default_downloader
        try:
            payload = fetch(qr_url, float(agent_config.request_timeout_seconds))
        except (requests.RequestException, ValueError, OSError) as exc:
            logger.warning("remote_access_qr_download_failed slot=%s reason=%s", slot_id, exc.__class__.__name__)
            return _place_result(
                ok=False,
                http_status=422,
                job_id=request.job_id,
                serial=serial,
                error="qr_download_failed",
                message="QR image could not be downloaded",
            )
    downloaded_size = len(payload)
    if downloaded_size <= 0:
        return _place_result(
            ok=False,
            http_status=422,
            job_id=request.job_id,
            serial=serial,
            downloaded_size=0,
            error="qr_download_failed",
            message="QR download was empty",
        )
    if downloaded_size > MAX_QR_IMAGE_BYTES:
        return _place_result(
            ok=False,
            http_status=422,
            job_id=request.job_id,
            serial=serial,
            downloaded_size=downloaded_size,
            error="qr_image_too_large",
            message="QR image exceeds the maximum allowed size",
        )
    extension = _image_extension(payload)
    if extension is None:
        return _place_result(
            ok=False,
            http_status=422,
            job_id=request.job_id,
            serial=serial,
            downloaded_size=downloaded_size,
            error="qr_not_an_image",
            message="QR payload is not a PNG, JPG, or WEBP image",
        )

    runner = command_runner or AdbCommandRunner(adb_path, agent_config.provisioning_timeout_seconds)
    try:
        state = runner.run(serial, ["get-state"]).stdout
    except AdbCommandError as exc:
        return _place_result(
            ok=False,
            http_status=409,
            job_id=request.job_id,
            serial=serial,
            downloaded_size=downloaded_size,
            error="device_offline",
            message="ADB device is offline",
        )
    if state != "device":
        return _place_result(
            ok=False,
            http_status=409,
            job_id=request.job_id,
            serial=serial,
            downloaded_size=downloaded_size,
            error="device_offline",
            message="ADB device is offline",
        )

    remote_name = remote_qr_filename(rental_id, request.job_id, extension)
    media_dir = qr_placement_dir(
        slot_id=slot_id,
        serial=serial,
        payload=request.payload,
        manufacturer=_manufacturer_from_adb(runner, serial),
    )
    remote_path = f"{media_dir}/{remote_name}"
    tmp_path: str | None = None
    remote_size: int | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="mobirent_qr_", suffix=f".{extension}", delete=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            tmp_path = handle.name
        local_size = os.path.getsize(tmp_path)
        if local_size <= 0:
            return _place_result(
                ok=False,
                http_status=422,
                job_id=request.job_id,
                serial=serial,
                destination=remote_path,
                downloaded_size=downloaded_size,
                remote_size=0,
                error="qr_push_failed",
                message="temporary QR file was empty before ADB push",
            )
        runner.run(serial, ["shell", "mkdir", "-p", media_dir])
        runner.run(serial, ["push", tmp_path, remote_path])
        try:
            listed = runner.run(serial, ["shell", "ls", remote_path]).stdout
        except AdbCommandError as exc:
            raise AdbCommandError("qr_not_on_device") from exc
        if remote_name not in (listed or "") and remote_path not in (listed or ""):
            raise AdbCommandError("qr_not_on_device")
        remote_size = _read_remote_byte_size(runner, serial, remote_path)
        if remote_size is None:
            raise AdbCommandError("qr_not_on_device")
        if remote_size <= 0:
            raise AdbCommandError("qr_zero_byte")
    except AdbCommandError as exc:
        reason = str(exc)
        if reason == "qr_zero_byte":
            error = "qr_zero_byte"
            message = "remote QR file size is zero"
        elif reason == "qr_not_on_device" or "qr_not_on_device" in reason:
            error = "qr_not_on_device"
            message = "remote QR file is missing after ADB push"
        else:
            error = "qr_push_failed"
            message = "ADB push of QR image failed"
        logger.warning("remote_access_qr_push_failed slot=%s error=%s", slot_id, error)
        return _place_result(
            ok=False,
            http_status=422,
            job_id=request.job_id,
            serial=serial,
            destination=remote_path,
            downloaded_size=downloaded_size,
            remote_size=remote_size if error == "qr_zero_byte" else None,
            error=error,
            message=message,
        )
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    media_scanned = _media_scan(runner, serial, remote_path)
    oem = detect_device_oem(runner, serial, slot_id)
    gallery_aliases = copy_renter_gallery_aliases(runner, serial, remote_path, oem=oem)
    baseline = capture_esim_baseline(runner, serial)
    disable_native_cameras(runner, serial, oem)
    wake_and_unlock_device(runner, serial)
    launched = launch_esim_setup_screen(runner, serial, oem)
    component = read_foreground_component(runner, serial)
    verified = oem_setup_screen_is_open(component, oem)
    prompt = handoff_prompt_for_oem(oem)
    extra = {
        "device_oem": oem,
        "setup_ui_state": SETUP_UI_AWAITING if verified else SETUP_UI_WORKING,
        "setup_ui_prompt": prompt if verified else "",
        "setup_human_click": "Add SIM" if oem == OEM_PIXEL else "Add eSIM",
        "esim_setup_launched": launched,
        "foreground_activity": component,
        **baseline,
    }

    logger.info(
        "esim_qr_placed slot=%s job_id=%s downloaded_size=%s remote_size=%s media_scanned=%s oem=%s",
        slot_id,
        request.job_id,
        downloaded_size,
        remote_size,
        media_scanned,
        oem,
    )
    return _place_result(
        ok=True,
        http_status=200,
        job_id=request.job_id,
        serial=serial,
        destination=remote_path,
        downloaded_size=downloaded_size,
        remote_size=remote_size,
        placed=True,
        media_scanned=media_scanned,
        gallery_aliases=gallery_aliases,
        message=f"qr_placed:{remote_path};media_scanned={str(media_scanned).lower()}",
        extra=extra,
    )


def run_remote_access_activation_status(
    *,
    adb_path: str,
    slot_map: dict[int, str],
    request: FarmTaskRequest,
    agent_config: AgentConfig | None,
    command_runner: AdbCommandRunner | None = None,
) -> FarmTaskResult:
    """Read-only SIM/eSIM observation. No provision_esim, no profile mutation.

    Enable-flag only on the Farm. Slot identity comes from the VPS-authorized
    ``farm_slot_id`` resolved through ``slot_map.json``. Does not apply
    ``REMOTE_ACCESS_POC_SLOT_IDS``.
    """
    slot_id = int(request.farm_slot_id)
    gated = _place_qr_config_gate(agent_config)
    if gated is not None:
        return gated
    serial = str(slot_map.get(slot_id) or "").strip()
    if not serial:
        return FarmTaskResult(ok=False, http_status=404, error="slot_not_found")
    timeout = float(getattr(agent_config, "provisioning_timeout_seconds", 30.0) or 30.0)
    runner = command_runner or AdbCommandRunner(adb_path, timeout)
    try:
        state = runner.run(serial, ["get-state"]).stdout
    except AdbCommandError as exc:
        return FarmTaskResult(ok=False, http_status=409, error="device_offline", message=str(exc))
    if state != "device":
        return FarmTaskResult(ok=False, http_status=409, error="device_offline", message=f"ADB state is {state or 'unknown'}")
    from domain.verification import evaluate_activation
    from infrastructure.adb_four_layer import collect_four_layer_verification

    snapshot = collect_four_layer_verification(runner, serial)
    verdict = evaluate_activation(snapshot)
    baseline = capture_esim_baseline(runner, serial)
    evidence = {
        "verdict": verdict.value,
        "esim_profile_present": snapshot.subscription.subscription_present,
        "esim_enabled": snapshot.settings_lpa.euicc_enabled,
        "network_registered": snapshot.telephony.data_registered,
        "cellular": snapshot.connectivity.cellular_transport_available,
        "observation_complete": snapshot.observation_complete,
        "esim_current_token": baseline.get("esim_current_token"),
    }
    logger.info("remote_access_activation_observed slot=%s verdict=%s", slot_id, verdict.value)
    return FarmTaskResult(ok=True, http_status=200, details=evidence)
