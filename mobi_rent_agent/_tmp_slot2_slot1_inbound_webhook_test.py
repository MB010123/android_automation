"""Controlled Slot 2 → Slot 1 inbound webhook test (one SMS)."""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

CAP_DIR = ROOT / "logs" / "voidfix_inbound_webhook_captures"
OUTBOX = ROOT / "logs" / "sms_outbox.sqlite"
REPORT = ROOT / "_tmp_slot2_slot1_inbound_webhook_report.json"
NGROK_REQ = "http://127.0.0.1:4040/api/requests/http?limit=100"

SRC_SLOT = 2
SRC_VF = "1389"
SRC_SERIAL = "19141FDF6OO8T9"
SRC_SIM2 = "+16514722709"
DST_SLOT = 1
DST_VF = "1386"
DST_SERIAL = "18171FDF6005WG"
DST_SIM2 = "+19522287088"
DEST = DST_SIM2
WEBHOOK_WAIT = 300


def read_allowlist_raw() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("VOIDFIX_RECIPIENT_ALLOWLIST="):
            return line.split("=", 1)[1].strip()
    return ""


def outbox_summary() -> dict:
    if not OUTBOX.is_file():
        return {"total": 0, "non_terminal": 0}
    con = sqlite3.connect(OUTBOX)
    total = con.execute("SELECT COUNT(*) FROM outbound_messages").fetchone()[0]
    non = con.execute(
        """
        SELECT COUNT(*) FROM outbound_messages
        WHERE status NOT IN ('delivered','failed','blocked','dry_run')
           OR (final_status IS NOT NULL AND final_status NOT IN ('delivered','failed','timeout'))
        """
    ).fetchone()[0]
    con.close()
    return {"total": total, "non_terminal": non}


def ngrok_post_inbound(since: float) -> list[dict]:
    try:
        with urllib.request.urlopen(NGROK_REQ, timeout=8) as resp:
            data = json.loads(resp.read().decode())
    except OSError:
        return []
    hits = []
    for req in data.get("requests") or []:
        r = req.get("request") or {}
        if (r.get("method") or "").upper() != "POST":
            continue
        if "/voidfix/inbound" not in (r.get("uri") or ""):
            continue
        start = req.get("start") or ""
        hits.append({"start": start, "status": (req.get("response") or {}).get("status")})
    return hits


def flatten_keys(obj: object, prefix: str = "") -> list[str]:
    keys: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            keys.append(path)
            keys.extend(flatten_keys(v, path))
    elif isinstance(obj, list) and obj:
        keys.extend(flatten_keys(obj[0], f"{prefix}[0]"))
    return keys


def inspect_payload_fields(payload: object) -> dict:
    """Describe fields without assuming VoidFix schema."""
    notes: dict = {"top_level_type": type(payload).__name__, "field_paths": flatten_keys(payload)}
    # Walk first message-like dict
    from infrastructure.voidfix_api import _coerce_message_list, SmsGatewayError

    try:
        msgs = _coerce_message_list(payload)
    except SmsGatewayError:
        msgs = []
    notes["message_object_count"] = len(msgs)
    if msgs and isinstance(msgs[0], dict):
        m0 = msgs[0]
        notes["first_message_keys"] = sorted(m0.keys())
        notes["candidate_sender_keys"] = [k for k in m0 if k.lower() in {
            "number", "from", "from_number", "sender", "source"
        }]
        notes["candidate_body_keys"] = [k for k in m0 if k.lower() in {
            "message", "text", "body", "content"
        }]
        notes["candidate_device_keys"] = [k for k in m0 if "device" in k.lower()]
        notes["candidate_dest_keys"] = [k for k in m0 if k.lower() in {
            "to", "to_number", "recipient", "destination"
        }]
        notes["candidate_time_keys"] = [k for k in m0 if any(
            x in k.lower() for x in ("date", "time", "received", "timestamp")
        )]
    return notes


def main() -> int:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    allowlist_original = read_allowlist_raw()
    report: dict = {
        "run_id": run_id,
        "phase1": {
            "allowlist_original": allowlist_original,
            "slot_mappings_ok": True,
            "source": {"slot": SRC_SLOT, "vf": SRC_VF, "serial": SRC_SERIAL, "sim2": SRC_SIM2},
            "destination": {"slot": DST_SLOT, "vf": DST_VF, "serial": DST_SERIAL, "sim2": DST_SIM2},
            "outbox_before": outbox_summary(),
        },
        "phase2": {"allowlist_change_required": False, "allowlist_after_phase2": allowlist_original},
    }

    hub = "+19522287088"
    if hub not in allowlist_original.replace(" ", "").split(",") and hub not in allowlist_original:
        report["phase2"]["allowlist_change_required"] = True
        report["error"] = "hub not on allowlist; test aborted without send"
        REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return 2

    CAP_DIR.mkdir(parents=True, exist_ok=True)
    captures_before = {p.name: p.stat().st_mtime for p in CAP_DIR.glob("inbound_*.json")}
    send_mark = time.time()

    from application.sms_factory import build_sms_dispatch_service, create_durable_sms_outbox
    from infrastructure.config import load_config
    from infrastructure.voidfix_devices import load_voidfix_device_map

    config = load_config(env_file=str(ROOT / ".env"))
    outbox = create_durable_sms_outbox()
    service = build_sms_dispatch_service(config, outbox=outbox)
    if service is None:
        report["error"] = "SmsDispatchService unavailable"
        REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return 2

    body = f"Mobi-Rent inbound webhook TEST slot2-to-slot1 run={run_id}"
    idem = f"slot2-slot1-inbound-{run_id}"
    t_send = time.time()
    result = service.send_for_slot(
        SRC_SLOT,
        DEST,
        body,
        idempotency_key=idem,
        expected_voidfix_device_id=SRC_VF,
    )
    record = outbox.get(idem)
    report["phase4_outbound"] = {
        "send_success": result.success,
        "send_error": result.error,
        "provider_message_id": result.provider_message_id,
        "idempotency_key": idem,
        "source_slot": SRC_SLOT,
        "destination": DEST,
        "message_body": body,
        "sent_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    if record:
        report["phase4_outbound"].update(
            {
                "outbox_status": record.status.value,
                "final_status": record.final_status.value if record.final_status else None,
                "provider_delivered_date": record.provider_delivered_date,
            }
        )

    report["phase5_outbound_lifecycle"] = {
        "accepted": bool(result.success and result.provider_message_id),
        "sent_or_better": record.final_status.value in {"sent", "delivered", "accepted"}
        if record and record.final_status
        else bool(result.success),
        "delivered": record.final_status.value == "delivered"
        if record and record.final_status
        else False,
    }

    # Phase 6: wait for webhook
    deadline = time.time() + WEBHOOK_WAIT
    capture_path: Path | None = None
    webhook_status: str | None = None
    while time.time() < deadline:
        for p in CAP_DIR.glob("inbound_*.json"):
            if p.name not in captures_before and p.stat().st_mtime >= send_mark - 1:
                capture_path = p
                break
        posts = ngrok_post_inbound(send_mark)
        if posts:
            webhook_status = str(posts[-1].get("status"))
        if capture_path is not None:
            break
        time.sleep(2.0)

    device_map = load_voidfix_device_map(config.voidfix_devices_path or ROOT / "voidfix_devices.json")
    reverse = {str(v): int(k) for k, v in device_map.items()}

    report["phase6_webhook"] = {
        "webhook_received": capture_path is not None,
        "webhook_http_status": webhook_status,
        "capture_file": capture_path.name if capture_path else None,
        "wait_seconds": WEBHOOK_WAIT,
    }

    if capture_path is None:
        report["phase6_webhook"]["error"] = (
            f"VoidFix did not send a webhook during the {WEBHOOK_WAIT}s test window"
        )
        report["phase9_allowlist_restore"] = {
            "changed_during_test": False,
            "restored": True,
            "current": read_allowlist_raw(),
        }
        report["final"] = _final_summary(report)
        REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return 1

    raw_capture = json.loads(capture_path.read_text(encoding="utf-8"))
    payload = raw_capture.get("payload")
    field_inspection = inspect_payload_fields(payload)
    ingest_error = None
    parsed = []
    try:
        parsed = service.ingest_inbound(payload)
    except Exception as exc:
        ingest_error = str(exc)

    device_ids = [m.device_id for m in parsed]
    mapped_slots = [reverse.get(str(d)) for d in device_ids if d]
    # Destination mapping: receiving device must be VF 1386 / slot 1
    dest_device_ok = any(str(d) == DST_VF for d in device_ids)
    slot1_ok = DST_SLOT in mapped_slots or (device_ids and reverse.get(str(device_ids[0])) == DST_SLOT)

    report["phase6_payload_inspection"] = field_inspection
    report["phase7_mapping"] = {
        "parsed_device_ids": device_ids,
        "mapped_slots": mapped_slots,
        "expected_destination_vf": DST_VF,
        "expected_destination_slot": DST_SLOT,
        "destination_device_identified": dest_device_ok,
        "mapped_to_slot1": slot1_ok,
        "wrong_source_slot2_only": reverse.get(str(device_ids[0])) == SRC_SLOT if device_ids else False,
    }
    report["phase8_ingest"] = {
        "ingest_ok": ingest_error is None and len(parsed) >= 1,
        "ingest_error": ingest_error,
        "parsed_count": len(parsed),
        "senders": [m.from_number for m in parsed],
        "message_lengths": [len(m.message or "") for m in parsed],
        "received_at": [m.received_at for m in parsed],
    }
    report["phase8_ingest"]["body_contains_test_token"] = any(
        run_id in (m.message or "") for m in parsed
    )

    report["phase9_allowlist_restore"] = {
        "changed_during_test": False,
        "restored": read_allowlist_raw() == allowlist_original,
        "current": read_allowlist_raw(),
    }
    report["inbound_persistence"] = {
        "raw_capture_file": str(capture_path.relative_to(ROOT)),
        "database_persistence": "NOT IMPLEMENTED",
    }
    report["final"] = _final_summary(report)
    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report["final"], indent=2))
    return 0 if report["final"]["overall_inbound_webhook_test"] == "PASS" else 1


def _final_summary(report: dict) -> dict:
    ob = report.get("phase4_outbound") or {}
    p5 = report.get("phase5_outbound_lifecycle") or {}
    p6 = report.get("phase6_webhook") or {}
    p7 = report.get("phase7_mapping") or {}
    p8 = report.get("phase8_ingest") or {}
    p9 = report.get("phase9_allowlist_restore") or {}
    outbound_pass = bool(ob.get("send_success") and ob.get("provider_message_id"))
    webhook_pass = bool(p6.get("webhook_received"))
    parse_pass = bool(p8.get("ingest_ok"))
    dest_pass = bool(p7.get("destination_device_identified"))
    slot_pass = bool(p7.get("mapped_to_slot1")) and not p7.get("wrong_source_slot2_only")
    capture_pass = webhook_pass and bool(p6.get("capture_file"))
    overall = all([outbound_pass, webhook_pass, parse_pass, dest_pass, slot_pass, capture_pass])
    return {
        "test": "Slot 2 → Slot 1",
        "outbound_sms": "PASS" if outbound_pass else "FAIL",
        "provider_sms_id": ob.get("provider_message_id"),
        "inbound_webhook_received": "PASS" if webhook_pass else "FAIL",
        "webhook_http_status": p6.get("webhook_http_status"),
        "actual_voidfix_payload_received": "YES" if webhook_pass else "NO",
        "payload_parsed": "PASS" if parse_pass else "FAIL",
        "destination_device_identified": "PASS" if dest_pass else "FAIL",
        "mapped_to_slot1_vf_1386": "PASS" if slot_pass else "FAIL",
        "inbound_message_captured": "PASS" if capture_pass else "FAIL",
        "inbound_database_persistence": "NOT IMPLEMENTED",
        "allowlist_restored": "PASS" if p9.get("restored") else "FAIL",
        "sms_sent": 1 if ob.get("send_success") else 0,
        "automatic_retries": 0,
        "overall_inbound_webhook_test": "PASS" if overall else "FAIL",
        "webhook_error": p6.get("error"),
    }


if __name__ == "__main__":
    raise SystemExit(main())
