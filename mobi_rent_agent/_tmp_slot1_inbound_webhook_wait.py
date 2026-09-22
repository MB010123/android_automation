"""Wait once for VoidFix webhook POST (test only). No SMS sent."""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CAP = ROOT / "logs" / "voidfix_inbound_webhook_captures"
NGROK = "http://127.0.0.1:4040/api/requests/http?limit=50"
TIMEOUT = 300
POLL = 2.0
SLOT = 1
VF = "1386"
SIM2 = "+19522287088"


def ngrok_posts() -> list[dict]:
    try:
        with urllib.request.urlopen(NGROK, timeout=5) as resp:
            data = json.loads(resp.read().decode())
    except OSError:
        return []
    out: list[dict] = []
    for req in data.get("requests") or []:
        r = req.get("request") or {}
        uri = r.get("uri") or ""
        method = (r.get("method") or "").upper()
        if method == "POST" and "/voidfix/inbound" in uri:
            out.append(req)
    return out


def main() -> int:
    CAP.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in CAP.glob("inbound_*.json")}
    deadline = time.time() + TIMEOUT
    print(f"waiting up to {TIMEOUT}s for webhook POST (send external SMS to {SIM2} now)", flush=True)
    while time.time() < deadline:
        after = {p.name for p in CAP.glob("inbound_*.json")}
        new_files = sorted(after - before)
        if new_files:
            path = CAP / new_files[-1]
            break
        posts = ngrok_posts()
        if posts:
            # capture may lag; still wait for file or use ngrok body
            time.sleep(1.0)
            after = {p.name for p in CAP.glob("inbound_*.json")}
            new_files = sorted(after - before)
            if new_files:
                path = CAP / new_files[-1]
                break
        time.sleep(POLL)
    else:
        posts = ngrok_posts()
        report = {
            "webhook_received": False,
            "error": f"no POST to /voidfix/inbound within {TIMEOUT}s",
            "ngrok_post_count": len(posts),
        }
        out = ROOT / "_tmp_slot1_inbound_webhook_report.json"
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 1

    raw = json.loads(path.read_text(encoding="utf-8"))
    payload = raw.get("payload")
    from infrastructure.voidfix_devices import load_voidfix_device_map
    from infrastructure.config import load_config
    from application.sms_factory import build_sms_dispatch_service

    config = load_config(env_file=str(ROOT / ".env"))
    device_map = load_voidfix_device_map(config.voidfix_devices_path or ROOT / "voidfix_devices.json")
    reverse = {str(v): int(k) for k, v in device_map.items()}

    service = build_sms_dispatch_service(config)
    parsed = []
    ingest_error = None
    if service is not None:
        try:
            parsed = service.ingest_inbound(payload)
        except Exception as exc:
            ingest_error = str(exc)

    device_ids = [m.device_id for m in parsed]
    mapped_slot = reverse.get(str(device_ids[0])) if device_ids else None

    report = {
        "capture_file": path.name,
        "webhook_received": True,
        "payload_parsed": bool(parsed) and ingest_error is None,
        "ingest_error": ingest_error,
        "parsed_count": len(parsed),
        "from_numbers": [m.from_number for m in parsed],
        "messages_redacted_len": [len(m.message or "") for m in parsed],
        "device_ids": device_ids,
        "voidfix_device_identified": bool(device_ids and device_ids[0]),
        "mapped_slot": mapped_slot,
        "expected_slot": SLOT,
        "expected_vf": VF,
        "slot_mapping_ok": mapped_slot == SLOT and str(device_ids[0] if device_ids else "") == VF,
        "inbound_persisted": path.is_file(),
        "persist_note": "raw JSON in logs/voidfix_inbound_webhook_captures; no separate inbound DB in production",
    }
    out = ROOT / "_tmp_slot1_inbound_webhook_report.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report.get("slot_mapping_ok") and report.get("payload_parsed") else 1


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
