"""Apply GADS MJPEG 60/12/50 globally and to idle bays. Does not restart GADS.

Usage (Farm PC):
  python tools/apply_gads_stream_settings.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.gads_stream_settings import (  # noqa: E402
    JPEG_QUALITY,
    SCALING_FACTOR,
    TARGET_FPS,
    device_stream_settings_payload,
    global_stream_settings_payload,
    matches_live_info,
)
from infrastructure.gads_remote_access import GadsHubClient  # noqa: E402
from infrastructure.gads_workspaces import load_gads_workspace_map  # noqa: E402

HUB = os.getenv("REMOTE_ACCESS_PLATFORM_URL", "http://100.118.88.3:10000").rstrip("/")
PROVIDER = os.getenv("GADS_PROVIDER_URL", "http://100.118.88.3:10001").rstrip("/")
SECRETS = Path(os.getenv("GADS_SECRETS_DIR", r"C:\gads\secrets"))
SLOT1_UDID = "18171FDF6005WG"
MONGO_URI = os.getenv("GADS_MONGO_URI", "mongodb://127.0.0.1:27017")


def _unwrap_info(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    if isinstance(result, dict):
        return result
    return payload


def _provider_info(udid: str) -> dict[str, Any]:
    url = f"{PROVIDER}/device/{quote(udid, safe='')}/info"
    try:
        response = requests.get(url, timeout=6)
        response.raise_for_status()
        return _unwrap_info(response.json())
    except (requests.RequestException, ValueError):
        return {}


def _live_in_use(client: GadsHubClient, workspaces: dict[int, str]) -> set[str]:
    busy: set[str] = set()
    seen: set[str] = set()
    for workspace in workspaces.values():
        text = str(workspace or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        try:
            devices = client.live_devices(text)
        except Exception:
            continue
        for live in devices:
            info = live.get("info") if isinstance(live.get("info"), dict) else {}
            udid = str(info.get("udid") or live.get("udid") or "").strip()
            if udid and (live.get("in_use") is True or str(live.get("in_use_by") or "").strip()):
                busy.add(udid)
    return busy


def _clear_device_overrides(udids: list[str]) -> str:
    try:
        from pymongo import MongoClient
    except ImportError:
        return "pymongo_missing"
    try:
        mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=4000)
        coll = mongo["gads"]["device_stream_settings"]
        if udids:
            result = coll.delete_many({"udid": {"$in": udids}})
        else:
            result = coll.delete_many({})
        mongo.close()
        return f"deleted={int(getattr(result, 'deleted_count', 0))}"
    except Exception as exc:
        return f"mongo_error:{exc.__class__.__name__}"


def main() -> int:
    admin_user = os.getenv("REMOTE_ACCESS_ADMIN_USERNAME") or "mobirent-backend"
    pw_file = SECRETS / "gads_backend_admin_password.txt"
    if not pw_file.is_file():
        print("FAIL: missing GADS backend admin password file")
        return 1
    client = GadsHubClient(
        HUB,
        admin_username=admin_user,
        admin_password=pw_file.read_text(encoding="utf-8").strip(),
        timeout_seconds=20.0,
    )
    workspaces = load_gads_workspace_map(SECRETS / "gads_workspaces.json")
    global_payload = global_stream_settings_payload()
    device_payload = device_stream_settings_payload()
    print(
        f"target jpeg={JPEG_QUALITY} fps={TARGET_FPS} scale={SCALING_FACTOR} hub={HUB}"
    )
    client.update_global_stream_settings(global_payload)
    print("global-settings updated")

    registered = client.registered_devices()
    busy = _live_in_use(client, workspaces)
    applied: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    for raw in registered:
        if not isinstance(raw, dict):
            continue
        udid = str(raw.get("udid") or "").strip()
        if not udid:
            continue
        os_name = str(raw.get("os") or raw.get("platform") or "android").lower()
        if os_name and "ios" in os_name:
            continue
        if udid in busy:
            skipped.append(udid)
            print(f"skip in_use {udid}")
            continue
        try:
            client.update_device_stream_settings(udid, device_payload)
            applied.append(udid)
            print(f"applied {udid}")
        except Exception as exc:
            failed.append(udid)
            print(f"FAIL apply {udid} {exc.__class__.__name__}")

    cleared = _clear_device_overrides(applied + ([SLOT1_UDID] if SLOT1_UDID not in busy else []))
    print(f"device_stream_settings {cleared}")

    print("verify /device/{udid}/info")
    mismatches = 0
    check = sorted(set(applied))
    for udid in check:
        info = {}
        try:
            info = _unwrap_info(client.device_info(udid))
        except Exception:
            info = _provider_info(udid)
        ok = matches_live_info(info)
        fps = info.get("stream_target_fps")
        jpeg = info.get("stream_jpeg_quality")
        scale = info.get("stream_scaling_factor")
        print(f"  {udid} jpeg={jpeg} fps={fps} scale={scale} ok={ok}")
        if not ok:
            mismatches += 1
    print(json.dumps({"applied": len(applied), "skipped_busy": len(skipped), "failed": len(failed)}))
    if failed or mismatches:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
