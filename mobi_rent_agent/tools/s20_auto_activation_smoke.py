"""Live smoke test for the Samsung S20 zero-touch eSIM activation task.

Runs the real ``esim_auto_activation`` Farm task against Slot 21 with a
syntactically valid but unreachable activation code. Expected outcome:
``activation_failed`` (profile search / download fails on the dummy SM-DP+)
— proving the flow works on the real device without adding any profile.

Methods:
  --method ui (default): generates a QR for the dummy code, pushes it to
    /sdcard/Pictures/ (exactly like remote_access_place_qr), then runs the
    full One UI walk: SIM manager → Add eSIM → Other ways to connect →
    Scan QR code → Open Gallery → Pictures → newest image → Done.
  --method deeplink: passes the code via esimmanagerfromqr:// directly.

Usage: python tools/s20_auto_activation_smoke.py [--method ui|deeplink] [--code LPA:1$...]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.esim_auto_activation_farm_task import run_esim_auto_activation
from application.farm_task_types import FarmTaskRequest
from infrastructure.config import load_config

DEFAULT_CODE = "LPA:1$smdp.example.invalid$MOBIRENT-SMOKE-TEST"
SMOKE_QR_REMOTE = "/sdcard/Pictures/mobirent_esim_qr_smoke.png"


def _push_smoke_qr(adb_path: str, serial: str, code: str) -> None:
    import zxingcpp
    from PIL import Image

    bitmap = zxingcpp.write_barcode(zxingcpp.BarcodeFormat.QRCode, code, width=740, height=740)
    height, width = bitmap.shape
    image = Image.frombuffer("L", (width, height), bytes(memoryview(bitmap)), "raw", "L", 0, 1)
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp, "mobirent_esim_qr_smoke.png")
        image.save(local, format="PNG")
        subprocess.run([adb_path, "-s", serial, "push", str(local), SMOKE_QR_REMOTE], check=True)
    subprocess.run(
        [
            adb_path, "-s", serial, "shell", "am", "broadcast",
            "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
            "-d", f"file://{SMOKE_QR_REMOTE}",
        ],
        check=True,
    )


def _remove_smoke_qr(adb_path: str, serial: str) -> None:
    subprocess.run([adb_path, "-s", serial, "shell", "rm", "-f", SMOKE_QR_REMOTE], check=False)
    subprocess.run(
        [
            adb_path, "-s", serial, "shell", "am", "broadcast",
            "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
            "-d", "file:///sdcard/Pictures",
        ],
        check=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot", type=int, default=21)
    parser.add_argument("--code", default=DEFAULT_CODE)
    parser.add_argument("--method", choices=("ui", "deeplink"), default="ui")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()

    config = load_config()
    slot_map = {int(k): v for k, v in json.loads(Path(ROOT, "slot_map.json").read_text()).items()}
    serial = slot_map[args.slot]

    payload: dict[str, str] = {"rental_id": "smoke", "actor": "renter"}
    if args.method == "deeplink":
        payload["activation_code"] = args.code
    else:
        _push_smoke_qr(config.adb_path, serial, args.code)

    request = FarmTaskRequest(
        job_id="smoke-s20-auto-activation",
        task_type="esim_auto_activation",
        farm_slot_id=args.slot,
        payload=payload,
    )
    try:
        result = run_esim_auto_activation(
            adb_path=config.adb_path,
            slot_map=slot_map,
            request=request,
            agent_config=config,
            timeout_seconds=args.timeout,
        )
    finally:
        if args.method == "ui":
            _remove_smoke_qr(config.adb_path, serial)
    print(json.dumps({
        "ok": result.ok,
        "http_status": result.http_status,
        "error": result.error,
        "message": result.message,
        "details": result.details,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
