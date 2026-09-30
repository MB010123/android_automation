"""Farm Agent task: place a validated eSIM QR image in /sdcard/DCIM/Camera/.

Slot-1 remote-access POC only. The image is pushed with plain ``adb push``
so the customer can open it in the Android eSIM UI ("scan QR" -> pick from
gallery, or read the code from the picture) over the remote screen.

Explicitly NOT done here: no ``provision_esim``, no companion socket, no
EuiccManager, no LPA parsing, no WRITE_EMBEDDED_SUBSCRIPTIONS, no policy or
owner changes. The device only receives a picture file.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from io import BytesIO
from typing import Any, Callable

import requests

from application.farm_task_types import FarmTaskRequest, FarmTaskResult
from infrastructure.adb_companion import AdbCommandError, AdbCommandRunner
from infrastructure.config import AgentConfig
from infrastructure.esim_qr_security import esim_fetch_url_is_public_https, esim_fetch_url_is_safe

logger = logging.getLogger("farm_agent.remote_access_place_qr")


def _poc_slot_gate(agent_config: AgentConfig | None, slot_id: int) -> FarmTaskResult | None:
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

DCIM_CAMERA_DIR = "/sdcard/DCIM/Camera"
MAX_QR_IMAGE_BYTES = 5 * 1024 * 1024
_RENTAL_TOKEN_RE = re.compile(r"[^a-z0-9]")

Downloader = Callable[[str, float], bytes]


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


def remote_qr_filename(rental_id: str, job_id: str, extension: str) -> str:
    rental_token = _RENTAL_TOKEN_RE.sub("", str(rental_id).lower())[:12] or "rental"
    job_token = _RENTAL_TOKEN_RE.sub("", str(job_id).lower())[:8] or "job"
    return f"mobirent_esim_qr_{rental_token}_{job_token}.{extension}"


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
    gated = _poc_slot_gate(agent_config, slot_id)
    if gated is not None:
        return gated
    serial = str(slot_map.get(slot_id) or "").strip()
    if not serial:
        return FarmTaskResult(ok=False, http_status=404, error="slot_not_found")

    qr_url = str(request.payload.get("esim_qr_url") or "").strip()
    if not qr_url or not esim_fetch_url_is_public_https(qr_url):
        return FarmTaskResult(
            ok=False,
            http_status=422,
            error="invalid_assignment",
            message="esim_qr_url must be a public HTTPS URL",
        )
    prefixes = tuple(getattr(agent_config, "remote_access_qr_url_prefixes", ()) or ())
    if prefixes and not esim_fetch_url_is_safe(qr_url, allowed_url_prefixes=prefixes):
        return FarmTaskResult(
            ok=False,
            http_status=422,
            error="invalid_assignment",
            message="esim_qr_url must be a public HTTPS URL",
        )
    rental_id = str(request.payload.get("rental_id") or request.job_id)

    fetch = downloader or _default_downloader
    try:
        payload = fetch(qr_url, float(agent_config.request_timeout_seconds))
    except (requests.RequestException, ValueError, OSError) as exc:
        logger.warning("remote_access_qr_download_failed slot=%s reason=%s", slot_id, exc.__class__.__name__)
        return FarmTaskResult(ok=False, http_status=422, error="qr_download_failed")
    extension = _image_extension(payload)
    if extension is None:
        return FarmTaskResult(ok=False, http_status=422, error="qr_not_an_image")

    runner = command_runner or AdbCommandRunner(adb_path, agent_config.provisioning_timeout_seconds)
    try:
        state = runner.run(serial, ["get-state"]).stdout
    except AdbCommandError as exc:
        return FarmTaskResult(ok=False, http_status=409, error="device_offline", message=str(exc))
    if state != "device":
        return FarmTaskResult(ok=False, http_status=409, error="device_offline", message=f"ADB state is {state or 'unknown'}")

    remote_name = remote_qr_filename(rental_id, request.job_id, extension)
    remote_path = f"{DCIM_CAMERA_DIR}/{remote_name}"
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="mobirent_qr_", suffix=f".{extension}", delete=False) as handle:
            handle.write(payload)
            tmp_path = handle.name
        runner.run(serial, ["shell", "mkdir", "-p", DCIM_CAMERA_DIR])
        runner.run(serial, ["push", tmp_path, remote_path])
    except AdbCommandError as exc:
        logger.warning("remote_access_qr_push_failed slot=%s reason=%s", slot_id, exc.__class__.__name__)
        return FarmTaskResult(ok=False, http_status=422, error="qr_push_failed")
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    media_scanned = True
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
    except AdbCommandError:
        media_scanned = False

    logger.info("remote_access_qr_placed slot=%s job_id=%s path=%s", slot_id, request.job_id, remote_path)
    return FarmTaskResult(
        ok=True,
        http_status=200,
        message=f"qr_placed:{remote_path};media_scanned={str(media_scanned).lower()}",
    )


def run_remote_access_activation_status(
    *,
    adb_path: str,
    slot_map: dict[int, str],
    request: FarmTaskRequest,
    agent_config: AgentConfig | None,
    command_runner: AdbCommandRunner | None = None,
) -> FarmTaskResult:
    """Read-only SIM/eSIM observation. No provision_esim, no profile mutation."""
    slot_id = int(request.farm_slot_id)
    gated = _poc_slot_gate(agent_config, slot_id)
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
    evidence = {
        "verdict": verdict.value,
        "esim_profile_present": snapshot.subscription.subscription_present,
        "esim_enabled": snapshot.settings_lpa.euicc_enabled,
        "network_registered": snapshot.telephony.data_registered,
        "cellular": snapshot.connectivity.cellular_transport_available,
        "observation_complete": snapshot.observation_complete,
    }
    logger.info("remote_access_activation_observed slot=%s verdict=%s", slot_id, verdict.value)
    return FarmTaskResult(ok=True, http_status=200, details=evidence)
