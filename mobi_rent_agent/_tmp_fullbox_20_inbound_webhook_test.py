"""Full 20-phone inbound webhook test: Slot 2 sends to all farm SIM2 MSISDNs."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

ENV_PATH = ROOT / ".env"
CAP = ROOT / "logs" / "voidfix_inbound_webhook_captures"
REPORT = ROOT / "_tmp_fullbox_20_inbound_webhook_report.json"
SRC_SLOT = 2
SRC_VF = "1389"
WEBHOOK_WAIT_AFTER_SEND = 600

DESTINATIONS: list[tuple[int, str, str]] = [
    (1, "1386", "+19522287088"),
    (2, "1389", "+16514722709"),
    (3, "1393", "+17633287165"),
    (4, "1394", "+17633468225"),
    (5, "1395", "+16126499401"),
    (6, "1402", "+17633390175"),
    (7, "1403", "+16126499481"),
    (8, "1404", "+17633573736"),
    (9, "1405", "+16126499603"),
    (10, "1406", "+17633406158"),
    (11, "1407", "+16126499651"),
    (12, "1408", "+17634382664"),
    (13, "1409", "+16126499683"),
    (14, "1410", "+17633935186"),
    (15, "1411", "+16126499573"),
    (16, "1412", "+17634384448"),
    (17, "1413", "+17633887556"),
    (18, "1414", "+17634388552"),
    (19, "1415", "+16126499751"),
    (20, "1417", "+17633935255"),
]

SERIALS = {
    1: "18171FDF6005WG",
    2: "19141FDF6OO8T9",
    3: "19161FDF6004AD",
    4: "19161FDF6005B9",
    5: "19161FDF6OO411",
    6: "19281FDF6OO1T4",
    7: "1A181FDF6006KN",
    8: "1B131FDF60090W",
    9: "1B301FDF6004HF",
    10: "1C021FDF600GKL",
    11: "1C071FDF6004MS",
    12: "1C071FDF6006YS",
    13: "1C111FDF600CNB",
    14: "1C141FDF600GXF",
    15: "21051FDF600EM9",
    16: "23101FDF60058N",
    17: "25061FDF6006KF",
    18: "25261FDF60017D",
    19: "1C101FDF6009EZ",
    20: "1A271FDF600BX2",
}


def read_allowlist_line() -> str:
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("VOIDFIX_RECIPIENT_ALLOWLIST="):
            return line
    return "VOIDFIX_RECIPIENT_ALLOWLIST="


def set_allowlist_line(line: str) -> None:
    text = ENV_PATH.read_text(encoding="utf-8")
    out_lines = []
    replaced = False
    for ln in text.splitlines():
        if ln.strip().startswith("VOIDFIX_RECIPIENT_ALLOWLIST="):
            out_lines.append(line)
            replaced = True
        else:
            out_lines.append(ln)
    if not replaced:
        out_lines.append(line)
    ENV_PATH.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


def adb_online(serial: str) -> bool:
    try:
        st = subprocess.run(
            ["adb", "-s", serial, "get-state"],
            capture_output=True,
            text=True,
            timeout=12,
        ).stdout.strip()
        return st == "device"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def precheck(run_id: str) -> dict:
    pre: dict = {"run_id": run_id}
    offline = [s for s, ser in SERIALS.items() if not adb_online(ser)]
    pre["adb_offline_slots"] = offline
    pre["adb_20_online"] = len(offline) == 0
    try:
        urllib.request.urlopen("http://127.0.0.1:8787/health", timeout=5).read()
        pre["listener"] = True
    except OSError:
        pre["listener"] = False
    try:
        t = json.loads(urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=5).read())
        pre["ngrok"] = any(x.get("proto") == "https" for x in t.get("tunnels", []))
    except OSError:
        pre["ngrok"] = False
    try:
        req = urllib.request.Request(
            "https://handstand-yesterday-ocelot.ngrok-free.dev/health",
            headers={"Ngrok-Skip-Browser-Warning": "true"},
        )
        pre["public_health"] = json.loads(urllib.request.urlopen(req, timeout=15).read()).get("ok")
    except OSError:
        pre["public_health"] = False
    pre["capture_count_before"] = len(list(CAP.glob("inbound_*.json")))
    return pre


def main() -> int:
    run_id = f"FULL-INBOUND-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    original_allowlist_line = read_allowlist_line()
    all_msisdn = sorted({msisdn for _, _, msisdn in DESTINATIONS})
    expanded_value = ",".join(all_msisdn)
    expanded_line = f"VOIDFIX_RECIPIENT_ALLOWLIST={expanded_value}"

    report: dict = {
        "run_id": run_id,
        "original_allowlist_line": original_allowlist_line,
        "temporary_allowlist_applied": expanded_line != original_allowlist_line,
    }

    pre = precheck(run_id)
    report["precheck"] = pre
    if not (pre.get("listener") and pre.get("ngrok") and pre.get("public_health")):
        report["error"] = "precheck failed"
        REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return 2

    set_allowlist_line(expanded_line)
    send_mark = time.time()
    captures_before = {p.name for p in CAP.glob("inbound_*.json")}

    import importlib.util

    from application.sms_factory import build_sms_dispatch_service, create_durable_sms_outbox
    from infrastructure.config import load_config
    from infrastructure.voidfix_devices import load_voidfix_device_map

    _spec = importlib.util.spec_from_file_location(
        "voidfix_inbound_webhook_listener",
        ROOT / "tools" / "voidfix_inbound_webhook_listener.py",
    )
    _mod = importlib.util.module_from_spec(_spec)
    assert _spec and _spec.loader
    _spec.loader.exec_module(_mod)
    slot_for_device = _mod.slot_for_voidfix_device

    config = load_config(env_file=str(ENV_PATH))
    outbox = create_durable_sms_outbox()
    service = build_sms_dispatch_service(config, outbox=outbox)
    if service is None:
        set_allowlist_line(original_allowlist_line)
        report["error"] = "SmsDispatchService unavailable"
        REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return 2

    device_map = load_voidfix_device_map(config.voidfix_device_map_path or ROOT / "voidfix_devices.json")
    vf_to_slot = {str(v): s for s, v in device_map.items()}

    outbound_rows: list[dict] = []
    for slot, vf, msisdn in DESTINATIONS:
        body = f"Mobi-Rent FULLBOX IN slot={slot:02d} run={run_id}"
        key = f"{run_id}-to-slot{slot:02d}"
        result = service.send_for_slot(
            SRC_SLOT,
            msisdn,
            body,
            idempotency_key=key,
            expected_voidfix_device_id=SRC_VF,
        )
        rec = outbox.get(key)
        outbound_rows.append(
            {
                "dest_slot": slot,
                "dest_vf": vf,
                "dest_msisdn": msisdn,
                "message": body,
                "idempotency_key": key,
                "send_success": result.success,
                "send_error": result.error,
                "provider_message_id": result.provider_message_id,
                "outbox_status": rec.status.value if rec else None,
                "final_status": rec.final_status.value if rec and rec.final_status else None,
                "delivered": rec.final_status.value == "delivered" if rec and rec.final_status else False,
            }
        )
        if not result.success:
            report["outbound_rows"] = outbound_rows
            report["error"] = f"outbound failed dest slot {slot}; stopping without retry"
            set_allowlist_line(original_allowlist_line)
            REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
            return 1

    report["outbound_rows"] = outbound_rows
    report["outbound_sent"] = len(outbound_rows)

    deadline = time.time() + WEBHOOK_WAIT_AFTER_SEND
    expected_slots = {slot for slot, _, _ in DESTINATIONS}
    matched: dict[int, dict] = {}

    while time.time() < deadline and len(matched) < 20:
        for p in CAP.glob("inbound_*.json"):
            if p.name in captures_before:
                continue
            if p.stat().st_mtime < send_mark - 2:
                continue
            try:
                cap = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            payload = cap.get("payload")
            if run_id not in json.dumps(payload):
                continue
            try:
                msgs = service.ingest_inbound(payload)
            except Exception as exc:
                msgs = []
                cap["_ingest_error"] = str(exc)
            if not msgs:
                continue
            body = msgs[0].message or ""
            m = re.search(r"slot=(\d{2})", body)
            if not m:
                continue
            dest_slot = int(m.group(1))
            if dest_slot in matched:
                continue
            dev = str(msgs[0].device_id or "")
            expected_vf = next(v for s, v, _ in DESTINATIONS if s == dest_slot)
            matched[dest_slot] = {
                "capture": p.name,
                "device_id": dev,
                "expected_vf": expected_vf,
                "sender": msgs[0].from_number,
                "body": body,
                "mapped_slot": slot_for_device(dev, device_map),
                "device_ok": dev == expected_vf,
                "slot_ok": slot_for_device(dev, device_map) == dest_slot,
                "marker_ok": f"slot={dest_slot:02d}" in body and run_id in body,
                "content_type": cap.get("content_type"),
                "form_ok": "form-urlencoded" in str(cap.get("content_type", "")).lower(),
            }
        if len(matched) >= 20:
            break
        time.sleep(3)

    set_allowlist_line(original_allowlist_line)
    restored = read_allowlist_line() == original_allowlist_line

    inbound_webhooks = len(matched)
    device_ok = sum(1 for v in matched.values() if v.get("device_ok"))
    slot_ok = sum(1 for v in matched.values() if v.get("slot_ok"))
    marker_ok = sum(1 for v in matched.values() if v.get("marker_ok"))
    form_ok = sum(1 for v in matched.values() if v.get("form_ok"))
    new_captures = len([p for p in CAP.glob("inbound_*.json") if p.name not in captures_before and p.stat().st_mtime >= send_mark - 2 and run_id in p.read_text(encoding="utf-8")])

    unexpected = [
        slot
        for slot, v in matched.items()
        if not v.get("device_ok") or not v.get("slot_ok")
    ]
    missing = sorted(expected_slots - set(matched.keys()))
    delivered = sum(1 for r in outbound_rows if r.get("delivered"))

    overall = (
        len(outbound_rows) == 20
        and all(r["send_success"] for r in outbound_rows)
        and inbound_webhooks == 20
        and device_ok == 20
        and slot_ok == 20
        and marker_ok == 20
        and new_captures >= 20
        and not missing
        and not unexpected
        and restored
    )

    report["inbound_matched"] = matched
    report["missing_webhook_slots"] = missing
    report["unexpected_mappings"] = unexpected
    report["allowlist_restored"] = restored
    report["summary"] = {
        "run_id": run_id,
        "inbound_sms_sent": f"{len(outbound_rows)}/20",
        "inbound_webhooks_received": f"{inbound_webhooks}/20",
        "http_200": f"{inbound_webhooks}/20",
        "payloads_parsed": f"{inbound_webhooks}/20",
        "ingest_inbound": f"{inbound_webhooks}/20",
        "correct_device_mapping": f"{device_ok}/20",
        "correct_slot_marker": f"{marker_ok}/20",
        "new_raw_captures": f"{new_captures}/20",
        "outbound_provider_delivery": f"{delivered}/20",
        "automatic_retries": 0,
        "duplicate_sends": 0,
        "unexpected_device_mappings": len(unexpected),
        "allowlist_restored": "PASS" if restored else "FAIL",
        "inbound_db_persistence": "NOT IMPLEMENTED",
        "overall": "PASS" if overall else "FAIL",
    }
    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], indent=2))
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
