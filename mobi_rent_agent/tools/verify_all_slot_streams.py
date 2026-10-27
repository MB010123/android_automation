"""Farm-PC check: ADB + GADS live + first MJPEG JPEG for every mapped bay.

Does not print tokens, passwords, workspace ids, or activation codes.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infrastructure.adb_slot_status import load_slot_map  # noqa: E402
from infrastructure.gads_remote_access import (  # noqa: E402
    GADS_HUB_ADMIN_USERNAME,
    GadsHubClient,
    GadsRemoteAccessPlatform,
)
from infrastructure.gads_workspaces import load_gads_workspace_map  # noqa: E402

HUB = os.getenv("REMOTE_ACCESS_PLATFORM_URL", "http://100.118.88.3:10000").rstrip("/")
SECRETS = Path(os.getenv("GADS_SECRETS_DIR", r"C:\gads\secrets"))
JPEG_SOI = b"\xff\xd8"


def _model(serial: str) -> str:
    try:
        proc = subprocess.run(
            ["adb", "-s", serial, "shell", "getprop", "ro.product.model"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "?"
    return (proc.stdout or "").strip() or "?"


def _first_jpeg_bytes(response: requests.Response, limit: int = 256 * 1024) -> int:
    buf = bytearray()
    for chunk in response.iter_content(chunk_size=4096):
        if not chunk:
            continue
        buf.extend(chunk)
        if JPEG_SOI in buf:
            start = buf.find(JPEG_SOI)
            rest = bytes(buf[start:])
            end = rest.find(b"\xff\xd9")
            if end >= 0:
                return end + 2
            if len(rest) >= 2048:
                return len(rest)
        if len(buf) >= limit:
            return len(buf) if JPEG_SOI in buf else 0
    return len(buf) if JPEG_SOI in buf else 0


def _farm_health(token: str) -> dict:
    req = requests.get(
        "http://127.0.0.1:8790/agent/health",
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    req.raise_for_status()
    body = req.json()
    return body if isinstance(body, dict) else {}


def _farm_inspect(token: str, slot: int) -> tuple[str, str]:
    payload = {
        "type": "setup_session_inspect",
        "farm_slot_id": slot,
        "job_id": str(uuid.uuid4()),
        "payload": {"phase": "esim", "recover": False},
    }
    try:
        resp = requests.post(
            "http://127.0.0.1:8790/agent/tasks/run",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=40,
        )
    except requests.RequestException as exc:
        return "ERR", exc.__class__.__name__
    try:
        body = resp.json() if resp.content else {}
    except ValueError:
        return f"HTTP{resp.status_code}", "invalid_json"
    if not isinstance(body, dict):
        return f"HTTP{resp.status_code}", "bad_body"
    details = body.get("details") if isinstance(body.get("details"), dict) else {}
    if resp.status_code == 200 and body.get("ok"):
        return "ok", str(details.get("setup_ui_state") or "ok")
    return f"HTTP{resp.status_code}", str(body.get("error") or details.get("error_code") or "fail")


def main() -> int:
    slot_map = load_slot_map(ROOT / "slot_map.json")
    ws_path = SECRETS / "gads_workspaces.json"
    workspaces = load_gads_workspace_map(ws_path) if ws_path.is_file() else {}
    admin_user = os.getenv("REMOTE_ACCESS_ADMIN_USERNAME") or "mobirent-backend"
    pw_file = SECRETS / "gads_backend_admin_password.txt"
    if not pw_file.is_file():
        print("FAIL: missing GADS backend admin password file")
        return 1
    client = GadsHubClient(HUB, admin_username=admin_user, admin_password=pw_file.read_text().strip())
    fallback_ws = (SECRETS / "gads_workspace_id.txt").read_text().strip() if (SECRETS / "gads_workspace_id.txt").is_file() else ""
    platform = GadsRemoteAccessPlatform(client, workspace_id=fallback_ws, public_url=HUB)

    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
    farm_token = os.getenv("FARM_AGENT_API_TOKEN") or ""
    health = _farm_health(farm_token) if farm_token else {}
    by_slot = {int(item["slot_id"]): item for item in (health.get("slots") or []) if isinstance(item, dict)}

    registered = {str(d.get("udid") or ""): d for d in client.registered_devices()}
    print(
        f"{'Slot':<5} {'Serial':<16} {'Model':<12} {'ADB':<10} {'GADS':<12} {'Inspect':<10} {'Stream'}"
    )
    print("-" * 96)
    failures = 0
    for slot in sorted(slot_map):
        serial = slot_map[slot]
        farm = by_slot.get(int(slot)) or {}
        adb = str(farm.get("adb_state") or "?")
        adb_online = farm.get("online")
        if adb_online is True:
            adb_col = "device"
        elif adb:
            adb_col = adb
        else:
            adb_col = "?"
        inspect_ok, inspect_detail = _farm_inspect(farm_token, int(slot)) if farm_token else ("skip", "no_token")
        gads_col = "unregistered"
        jpeg = 0
        stream_col = "skip"
        try:
            status = platform.device_status(
                slot_id=int(slot),
                device_id=serial,
                workspace_id=workspaces.get(int(slot)) or fallback_ws,
                require_workspace=False,
            )
            if not status.registered:
                gads_col = "unregistered"
            elif status.online:
                gads_col = "live"
            else:
                gads_col = "offline"
            if status.registered:
                try:
                    resp = platform.open_mjpeg_stream(
                        device_id=serial,
                        platform_username=GADS_HUB_ADMIN_USERNAME,
                        platform_password="",
                    )
                    try:
                        jpeg = _first_jpeg_bytes(resp)
                    finally:
                        closer = getattr(resp, "close", None)
                        if callable(closer):
                            closer()
                    stream_col = f"jpeg {jpeg}B" if jpeg else "no-jpeg"
                except Exception as exc:  # noqa: BLE001
                    stream_col = exc.__class__.__name__[:24]
        except Exception as exc:  # noqa: BLE001
            gads_col = exc.__class__.__name__[:12]
            stream_col = "n/a"
        row_fail = (
            adb_col != "device"
            or inspect_ok != "ok"
            or gads_col != "live"
            or not str(stream_col).startswith("jpeg ")
        )
        if row_fail:
            failures += 1
        mark = "FAIL" if row_fail else "ok"
        print(
            f"{slot:<5} {serial:<16} {_model(serial)[:12]:<12} {adb_col:<10} {gads_col:<12} {inspect_ok:<10} {stream_col} {mark}"
        )
        _ = registered
    print("-" * 96)
    print(f"failures={failures} mapped={len(slot_map)} registered={len(registered)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
