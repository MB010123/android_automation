"""Farm Agent task: place a validated eSIM QR image in /sdcard/DCIM/Camera/.

Any Farm-mapped bay. Customer authorization (ownership + mapped bay) is
enforced on the VPS. This task resolves ``farm_slot_id`` through
``slot_map.json`` and ``adb push``es the picture so the customer can open
it in the Android eSIM UI. GADS POC allowlists are not applied here.

Explicitly NOT done here: no ``provision_esim``, no companion socket, no
EuiccManager, no LPA parsing, no WRITE_EMBEDDED_SUBSCRIPTIONS, no policy or
owner changes. The device only receives a picture file.
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


def _place_qr_config_gate(agent_config: AgentConfig | None) -> FarmTaskResult | None:
    """QR Camera push is not GADS-allowlisted. POC flag still arms the Farm task."""
    if agent_config is None:
        return FarmTaskResult(ok=False, http_status=503, error="agent_not_configured")
    if not getattr(agent_config, "remote_access_poc_enabled", False):
        return FarmTaskResult(ok=False, http_status=403, error="remote_access_poc_disabled")
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
    remote_path = f"{DCIM_CAMERA_DIR}/{remote_name}"
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
        runner.run(serial, ["shell", "mkdir", "-p", DCIM_CAMERA_DIR])
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

    logger.info(
        "esim_qr_placed slot=%s job_id=%s downloaded_size=%s remote_size=%s media_scanned=%s",
        slot_id,
        request.job_id,
        downloaded_size,
        remote_size,
        media_scanned,
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
