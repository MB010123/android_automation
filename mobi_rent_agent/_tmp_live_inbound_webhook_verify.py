"""One fresh Slot 2→Slot 1 SMS + live VoidFix webhook verification."""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

CAP = ROOT / "logs" / "voidfix_inbound_webhook_captures"
REPORT = ROOT / "_tmp_live_inbound_webhook_verify_report.json"
NGROK_TUNNELS = "http://127.0.0.1:4040/api/tunnels"
NGROK_REQ = "http://127.0.0.1:4040/api/requests/http?limit=80"
PUBLIC_HEALTH = "https://handstand-yesterday-ocelot.ngrok-free.dev/health"
WAIT = 300

SRC_SLOT = 2
SRC_VF = "1389"
DEST = "+19522287088"
DST_VF = "1386"
DST_SLOT = 1


def read_allowlist() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("VOIDFIX_RECIPIENT_ALLOWLIST="):
            return line.split("=", 1)[1].strip()
    return ""


def precheck() -> dict:
    out: dict = {"allowlist": read_allowlist(), "started_at_utc": datetime.now(timezone.utc).isoformat()}
    try:
        urllib.request.urlopen("http://127.0.0.1:8787/health", timeout=5).read()
        out["listener"] = "PASS"
    except OSError as exc:
        out["listener"] = f"FAIL: {exc}"
    try:
        tunnels = json.loads(urllib.request.urlopen(NGROK_TUNNELS, timeout=5).read())
        https = [t for t in tunnels.get("tunnels", []) if t.get("proto") == "https"]
        out["ngrok_online"] = "PASS" if https else "FAIL"
        out["ngrok_url"] = https[0].get("public_url") if https else None
    except OSError as exc:
        out["ngrok_online"] = f"FAIL: {exc}"
    try:
        req = urllib.request.Request(
            PUBLIC_HEALTH,
            headers={"Ngrok-Skip-Browser-Warning": "true"},
        )
        body = json.loads(urllib.request.urlopen(req, timeout=15).read())
        out["public_health"] = "PASS" if body.get("ok") else "FAIL"
    except OSError as exc:
        out["public_health"] = f"FAIL: {exc}"
    out["webhook_ready"] = "PASS" if out.get("listener") == "PASS" and out.get("ngrok_online") == "PASS" else "FAIL"
    return out


def ngrok_inbound_posts_after(since_iso_prefix: str) -> list[dict]:
    data = json.loads(urllib.request.urlopen(NGROK_REQ, timeout=10).read())
    hits = []
    for req in data.get("requests") or []:
        r = req.get("request") or {}
        if (r.get("method") or "").upper() != "POST":
            continue
        if "/voidfix/inbound" not in (r.get("uri") or ""):
            continue
        start = req.get("start") or ""
        if start < since_iso_prefix[:16]:  # rough filter by local date prefix
            continue
        resp = req.get("response") or {}
        hits.append(
            {
                "id": req.get("id"),
                "start": start,
                "status": resp.get("status"),
                "status_code": resp.get("status_code"),
            }
        )
    return hits


def main() -> int:
    pre = precheck()
    if pre.get("webhook_ready") != "PASS":
        REPORT.write_text(json.dumps({"precheck": pre, "error": "precheck failed"}, indent=2), encoding="utf-8")
        return 2

    CAP.mkdir(parents=True, exist_ok=True)
    captures_before = set(CAP.glob("inbound_*.json"))
    send_mark = time.time()
    since_local = datetime.now().astimezone().strftime("%Y-%m-%dT%H:")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    marker = f"Mobi-Rent LIVE inbound verification {run_id}"
    idem = f"live-inbound-verify-{run_id}"

    from application.sms_factory import build_sms_dispatch_service, create_durable_sms_outbox
    from infrastructure.config import load_config
    from infrastructure.voidfix_devices import load_voidfix_device_map
    import importlib.util

    _spec = importlib.util.spec_from_file_location(
        "voidfix_inbound_webhook_listener",
        ROOT / "tools" / "voidfix_inbound_webhook_listener.py",
    )
    _mod = importlib.util.module_from_spec(_spec)
    assert _spec and _spec.loader
    _spec.loader.exec_module(_mod)
    slot_for_voidfix_device = _mod.slot_for_voidfix_device

    config = load_config(env_file=str(ROOT / ".env"))
    outbox = create_durable_sms_outbox()
    service = build_sms_dispatch_service(config, outbox=outbox)
    if service is None:
        REPORT.write_text(json.dumps({"precheck": pre, "error": "no dispatch service"}, indent=2), encoding="utf-8")
        return 2

    result = service.send_for_slot(
        SRC_SLOT,
        DEST,
        marker,
        idempotency_key=idem,
        expected_voidfix_device_id=SRC_VF,
    )
    record = outbox.get(idem)
    outbound = {
        "send_success": result.success,
        "send_error": result.error,
        "provider_message_id": result.provider_message_id,
        "idempotency_key": idem,
        "outbox_status": record.status.value if record else None,
        "final_status": record.final_status.value if record and record.final_status else None,
    }

    deadline = time.time() + WAIT
    capture_path: Path | None = None
    ngrok_hit: dict | None = None
    while time.time() < deadline:
        new_caps = [p for p in CAP.glob("inbound_*.json") if p not in captures_before]
        if new_caps:
            capture_path = max(new_caps, key=lambda p: p.stat().st_mtime)
            if capture_path.stat().st_mtime >= send_mark - 2:
                break
        posts = ngrok_inbound_posts_after(since_local)
        for p in posts:
            if p.get("status_code") == 200:
                ngrok_hit = p
        if capture_path and ngrok_hit:
            break
        time.sleep(2)

    posts = ngrok_inbound_posts_after(since_local)
    if not ngrok_hit:
        for p in reversed(posts):
            if p.get("start"):
                ngrok_hit = p
                break

    verify: dict = {"webhook_received": capture_path is not None or bool(posts)}
    if capture_path:
        cap = json.loads(capture_path.read_text(encoding="utf-8"))
        payload = cap.get("payload")
        ct = cap.get("content_type") or cap.get("headers", {}).get("Content-Type", "")
        verify["content_type"] = ct
        verify["form_urlencoded"] = "form-urlencoded" in str(ct).lower()
        msgs = payload if isinstance(payload, list) else [payload]
        if isinstance(payload, dict) and "messages" in payload:
            msgs = payload["messages"]
        row = msgs[0] if isinstance(msgs, list) and msgs else {}
        verify["device_id_row"] = str(row.get("deviceID") or row.get("device_id") or "")
        verify["sender_row"] = str(row.get("number") or "")
        verify["message_has_marker"] = marker in str(row.get("message") or "")
        try:
            parsed = service.ingest_inbound(payload)
            verify["ingest_count"] = len(parsed)
            verify["ingest_sender"] = parsed[0].from_number if parsed else None
            verify["ingest_device"] = parsed[0].device_id if parsed else None
        except Exception as exc:
            verify["ingest_error"] = str(exc)
            verify["ingest_count"] = 0
        dm = load_voidfix_device_map(config.voidfix_device_map_path or ROOT / "voidfix_devices.json")
        verify["mapped_slot"] = slot_for_voidfix_device(verify.get("ingest_device") or verify["device_id_row"], dm)
    verify["ngrok_posts"] = posts
    verify["ngrok_hit"] = ngrok_hit
    verify["capture_file"] = capture_path.name if capture_path else None

    http_code = (ngrok_hit or {}).get("status_code")
    overall = (
        outbound.get("send_success")
        and verify.get("webhook_received")
        and http_code == 200
        and verify.get("form_urlencoded")
        and verify.get("ingest_count") == 1
        and verify.get("ingest_sender") == "+16514722709"
        and str(verify.get("ingest_device")) == "1386"
        and verify.get("mapped_slot") == DST_SLOT
        and verify.get("message_has_marker")
        and capture_path is not None
    )

    report = {
        "precheck": pre,
        "outbound": outbound,
        "verify": verify,
        "final": {
            "fresh_live_sms_sent": "PASS" if outbound.get("send_success") else "FAIL",
            "provider_sms_id": outbound.get("provider_message_id"),
            "webhook_received": "PASS" if verify.get("webhook_received") and capture_path else "FAIL",
            "webhook_http_response": http_code,
            "form_urlencoded_parsed": "PASS"
            if verify.get("form_urlencoded") and verify.get("ingest_count") == 1
            else "FAIL",
            "ingest_inbound": "PASS" if verify.get("ingest_count") == 1 else "FAIL",
            "sender_identified": "PASS" if verify.get("ingest_sender") == "+16514722709" else "FAIL",
            "device_1386": "PASS" if str(verify.get("ingest_device")) == "1386" else "FAIL",
            "mapped_slot_1": "PASS" if verify.get("mapped_slot") == 1 else "FAIL",
            "new_raw_capture": "PASS" if capture_path else "FAIL",
            "automatic_retries": 0,
            "sms_sent": 1 if outbound.get("send_success") else 0,
            "overall": "PASS" if overall else "FAIL",
            "marker": marker,
        },
    }
    if not verify.get("webhook_received") or not capture_path:
        report["final"]["error"] = f"No webhook/capture within {WAIT}s"

    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report["final"], indent=2))
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
