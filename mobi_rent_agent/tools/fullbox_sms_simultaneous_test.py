"""Full-box SMS test via production SmsDispatchService (20 slots).

Default is prepare-only. --execute runs Phase 1 outbound; --phase2 runs inbound after outbound PASS.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_CONFIG = ROOT / "_tmp_fullbox_test_config.json"
FINAL_REPORT = ROOT / "_tmp_fullbox_live_final_report.json"
PROTOTYPE_VF = "1385"
PROTOTYPE_SERIAL = "3C071JEHN14705"
HUB_MSISDN = "+19522287088"


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def adb_devices() -> dict[str, str]:
    try:
        out = subprocess.run(
            ["adb", "devices"],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {}
    result: dict[str, str] = {}
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            result[parts[0]] = parts[1]
    return result


def adb_state(serial: str) -> str:
    try:
        return subprocess.run(
            ["adb", "-s", serial, "get-state"],
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout.strip() or "unknown"
    except subprocess.TimeoutExpired:
        return "timeout"


def record_to_dict(record, row: dict) -> dict:
    from domain.farm import OutboundMessageStatus, SmsFinalStatus

    if record is None:
        return {"slot": row["slot"], "error": "no outbox record"}
    return {
        "slot_id": record.slot_id,
        "adb_serial": row["adb_serial"],
        "voidfix_device_id": row["voidfix_device_id"],
        "sim2_msisdn": row["sim2_msisdn"],
        "recipient": row["outbound_test_recipient"],
        "message_token": row.get("outbound_message"),
        "idempotency_key": record.idempotency_key,
        "provider_message_id": record.provider_message_id,
        "status": record.status.value,
        "final_status": record.final_status.value if record.final_status else None,
        "queued": record.final_status is SmsFinalStatus.QUEUED
        or record.status is OutboundMessageStatus.PENDING,
        "accepted": record.final_status in {SmsFinalStatus.ACCEPTED, SmsFinalStatus.SENT, SmsFinalStatus.DELIVERED}
        or record.status in {OutboundMessageStatus.CONFIRMED, OutboundMessageStatus.SENT, OutboundMessageStatus.DELIVERED},
        "sent": record.final_status in {SmsFinalStatus.SENT, SmsFinalStatus.DELIVERED}
        or record.status in {OutboundMessageStatus.SENT, OutboundMessageStatus.DELIVERED},
        "delivered": record.final_status is SmsFinalStatus.DELIVERED
        or record.status is OutboundMessageStatus.DELIVERED,
        "deliveredDate": record.provider_delivered_date,
        "sentDate": record.provider_sent_date,
        "error": record.error,
        "errorCode": record.provider_error_code,
        "accepted_at": record.accepted_at,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "duration_seconds": (record.updated_at - record.created_at) if record.updated_at and record.created_at else None,
    }


def send_one_slot(service, outbox, row: dict, run_id: str) -> dict:
    slot = int(row["slot"])
    vf = str(row["voidfix_device_id"])
    if vf == PROTOTYPE_VF or row["adb_serial"] == PROTOTYPE_SERIAL:
        return {"slot": slot, "success": False, "error": "prototype excluded"}
    to_number = row["outbound_test_recipient"]
    message = row.get("outbound_message") or f"Mobi-Rent fullbox OUT slot={slot:02d} run={run_id}"
    key = f"fullbox-{run_id}-slot{slot:02d}"
    t0 = time.time()
    result = service.send_for_slot(
        slot,
        to_number,
        message,
        idempotency_key=key,
        expected_voidfix_device_id=vf,
    )
    record = outbox.get(key)
    detail = record_to_dict(record, row)
    detail["send_success"] = result.success
    detail["send_error"] = result.error
    detail["wall_seconds"] = round(time.time() - t0, 3)
    return detail


def hub_send_inbound_adb(hub_serial: str, to_msisdn: str, body: str) -> dict:
    """Send SMS from hub handset (slot 1) via Messages intent; not SmsDispatchService (allowlist)."""
    uri = f"sms:{to_msisdn}"
    safe_body = body.replace("\\", "\\\\").replace('"', '\\"')
    # Single quoted shell command so spaces in sms_body are not split on device.
    shell_cmd = (
        f'am start -a android.intent.action.SENDTO -d "{uri}" '
        f'--es sms_body "{safe_body}" --ez exit_on_sent true'
    )
    try:
        proc = subprocess.run(
            ["adb", "-s", hub_serial, "shell", shell_cmd],
            capture_output=True,
            text=True,
            timeout=45,
        )
        return {
            "ok": proc.returncode == 0,
            "stdout": (proc.stdout or "")[:500],
            "stderr": (proc.stderr or "")[:500],
            "method": "adb_hub_SENDTO_exit_on_sent",
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "adb timeout", "method": "adb_hub_SENDTO_exit_on_sent"}


def _coerce_messages(payload: object) -> list[dict]:
    from infrastructure.voidfix_api import SmsGatewayError, _coerce_message_list

    try:
        raw = _coerce_message_list(payload)
    except SmsGatewayError:
        return []
    return [x for x in raw if isinstance(x, dict)]


def find_inbound_in_read_messages(
    messages: list[dict],
    *,
    voidfix_device_id: str,
    token: str,
    hub: str = HUB_MSISDN,
) -> list[dict]:
    hub_digits = re.sub(r"\D", "", hub)
    matches: list[dict] = []
    for row in messages:
        dev = str(row.get("deviceID") or row.get("deviceId") or row.get("device") or "")
        if dev and dev != str(voidfix_device_id):
            continue
        body = str(row.get("message") or row.get("text") or row.get("body") or "")
        if token not in body:
            continue
        sender = str(row.get("number") or row.get("from") or row.get("sender") or "")
        sender_digits = re.sub(r"\D", "", sender)
        msg_type = str(row.get("type") or row.get("direction") or row.get("msgType") or "").lower()
        if msg_type and msg_type not in {"received", "incoming", "in", "inbound", "1"}:
            continue
        if sender_digits and not sender_digits.endswith(hub_digits[-10:]):
            continue
        matches.append(row)
    return matches


def verify_inbound_adb(serial: str, token: str, hub: str = HUB_MSISDN) -> dict:
    """Read device SMS inbox via content provider (verified fallback)."""
    safe_token = token.replace("'", "")
    where = f"body LIKE '%{safe_token[:40]}%'"
    shell_cmd = (
        f"content query --uri content://sms/inbox "
        f"--projection address:body:date --where \"{where}\""
    )
    try:
        proc = subprocess.run(
            ["adb", "-s", serial, "shell", shell_cmd],
            capture_output=True,
            text=True,
            timeout=45,
        )
        text = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return {"received": False, "match_count": 0, "source": "adb_sms_inbox", "error": "timeout"}
    rows = [ln for ln in text.splitlines() if "Row:" in ln or "body=" in ln]
    hub_tail = re.sub(r"\D", "", hub)[-10:]
    matches = []
    for ln in rows:
        if token not in ln:
            continue
        digits = re.sub(r"\D", "", ln)
        if hub_tail and hub_tail in digits:
            matches.append(ln)
        elif token in ln:
            matches.append(ln)
    return {
        "received": len(matches) >= 1,
        "received_once": len(matches) == 1,
        "match_count": len(matches),
        "source": "adb_sms_inbox content://sms/inbox",
        "raw_head": text[:400],
    }


def find_inbound_loose(messages: list[dict], token: str, voidfix_device_id: str) -> list[dict]:
    """Fallback: match token + device without type filter."""
    out: list[dict] = []
    for row in messages:
        dev = str(row.get("deviceID") or row.get("deviceId") or row.get("device") or "")
        if dev and dev != str(voidfix_device_id):
            continue
        body = str(row.get("message") or row.get("text") or row.get("body") or "")
        if token in body:
            out.append(row)
    return out


def run_phase2_inbound(
    plan: dict,
    poller,
    vf_to_slot: dict[str, int],
    *,
    send: bool = True,
) -> list[dict]:
    slots = plan.get("slots") or []
    hub_row = next((s for s in slots if int(s["slot"]) == 1), None)
    if not hub_row:
        return [{"error": "no slot 1 hub row"}]
    hub_serial = hub_row["adb_serial"]
    inbound_results: list[dict] = []
    for row in sorted(slots, key=lambda x: int(x["slot"])):
        slot = int(row["slot"])
        body = row.get("inbound_reply_message") or ""
        dest = row["expected_inbound_reply_target"]
        send_meta = (
            hub_send_inbound_adb(hub_serial, dest, body)
            if send
            else {"skipped": True, "reason": "verify-only"}
        )
        if send:
            time.sleep(4.0)
        inbound_results.append(
            {
                "slot": slot,
                "adb_serial": row["adb_serial"],
                "expected_sim2": dest,
                "voidfix_device_id": row["voidfix_device_id"],
                "inbound_token": body,
                "hub_send": send_meta,
            }
        )

    if not send:
        for r in inbound_results:
            token = r["inbound_token"]
            vf = str(r["voidfix_device_id"])
            adb_v = verify_inbound_adb(r["adb_serial"], token)
            r["adb_inbox_verify"] = adb_v
            r["match_count"] = adb_v.get("match_count", 0)
            r["received"] = bool(adb_v.get("received"))
            r["received_once"] = bool(adb_v.get("received_once"))
            r["verification_source"] = adb_v.get("source", "adb_sms_inbox")
            r["matched_slot"] = vf_to_slot.get(vf) == r["slot"]
        return inbound_results

    deadline = time.time() + 180.0
    all_msgs: list[dict] = []
    fetch_err: str | None = None
    while time.time() < deadline:
        try:
            payload = poller.fetch_messages_payload()
            all_msgs = _coerce_messages(payload)
            fetch_err = None
        except Exception as exc:
            fetch_err = str(exc)
            time.sleep(10.0)
            continue
        pending = 0
        for r in inbound_results:
            token = r["inbound_token"]
            vf = str(r["voidfix_device_id"])
            matches = find_inbound_in_read_messages(all_msgs, voidfix_device_id=vf, token=token)
            if not matches:
                matches = find_inbound_loose(all_msgs, token, vf)
            if len(matches) != 1:
                pending += 1
        if pending == 0:
            break
        time.sleep(10.0)

    if fetch_err:
        for r in inbound_results:
            r["verify_error"] = f"read-messages fetch failed: {fetch_err}"
        return inbound_results

    for r in inbound_results:
        token = r["inbound_token"]
        vf = str(r["voidfix_device_id"])
        matches = find_inbound_in_read_messages(all_msgs, voidfix_device_id=vf, token=token)
        source = "read-messages.php fallback (VOIDFIX_INBOUND_ENDPOINT unset)"
        if not matches:
            matches = find_inbound_loose(all_msgs, token, vf)
            source = "read-messages.php loose token+device match"
        r["match_count"] = len(matches)
        r["received"] = len(matches) >= 1
        r["received_once"] = len(matches) == 1
        r["verification_source"] = source
        if matches:
            r["sample_row_id"] = str(matches[0].get("ID") or matches[0].get("id") or "")
        if not r["received_once"]:
            adb_v = verify_inbound_adb(r["adb_serial"], token)
            r["adb_inbox_verify"] = adb_v
            if adb_v.get("received_once"):
                r["received"] = True
                r["received_once"] = True
                r["match_count"] = adb_v.get("match_count", 1)
                r["verification_source"] = adb_v.get("source", "adb_sms_inbox")
        r["matched_slot"] = vf_to_slot.get(vf) == r["slot"]
    return inbound_results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--execute", action="store_true", help="Live Phase 1 outbound")
    parser.add_argument(
        "--phase2",
        action="store_true",
        help="Run Phase 2 inbound after successful Phase 1 (use with --execute)",
    )
    parser.add_argument(
        "--phase2-only",
        action="store_true",
        help="Skip Phase 1; run inbound hub replies + verification only (no new outbound)",
    )
    parser.add_argument(
        "--phase2-verify-only",
        action="store_true",
        help="Verify inbound only (no hub resend); use after Phase 2 sends already attempted",
    )
    args = parser.parse_args()

    if not args.config.is_file():
        print(f"Missing config {args.config}")
        return 2

    plan = json.loads(args.config.read_text(encoding="utf-8"))
    slots = plan.get("slots") or []
    if not args.execute and not args.phase2_only and not args.phase2_verify_only:
        print(json.dumps({"mode": "prepare-only", "slot_count": len(slots)}, indent=2))
        print("No SMS sent (--execute not set).")
        return 0

    from application.sms_factory import build_sms_dispatch_service, create_durable_sms_outbox
    from infrastructure.config import load_config

    config = load_config(env_file=str(ROOT / ".env"))
    outbox = create_durable_sms_outbox()
    service = build_sms_dispatch_service(config, outbox=outbox)
    if service is None:
        print("SmsDispatchService not configured")
        return 2

    run_id = plan.get("run_id") or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    serials = {int(s["slot"]): s["adb_serial"] for s in slots}
    adb_before = adb_devices()
    slot_adb_before = {slot: adb_state(ser) for slot, ser in serials.items()}

    outbound: list[dict] = []
    phase1_start = phase1_end = time.time()
    if args.phase2_only or args.phase2_verify_only:
        prior = FINAL_REPORT if FINAL_REPORT.is_file() else ROOT / "_tmp_fullbox_sms_simultaneous_report.json"
        if prior.is_file():
            prev = json.loads(prior.read_text(encoding="utf-8"))
            outbound = prev.get("phase1_outbound", {}).get("slots") or []
    else:
        phase1_start = time.time()
        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = {
                pool.submit(send_one_slot, service, outbox, row, run_id): int(row["slot"])
                for row in slots
            }
            for fut in as_completed(futures):
                outbound.append(fut.result())
        outbound.sort(key=lambda x: x.get("slot_id") or x.get("slot") or 0)
        phase1_end = time.time()

    adb_after = adb_devices()
    slot_adb_after = {slot: adb_state(ser) for slot, ser in serials.items()}
    adb_disconnects = []
    for slot, ser in serials.items():
        before = slot_adb_before.get(slot)
        after = slot_adb_after.get(slot)
        dev_line = adb_after.get(ser, "missing")
        if before != "device" or after != "device" or dev_line != "device":
            adb_disconnects.append(
                {"slot": slot, "serial": ser, "before": before, "after": after, "devices_line": dev_line}
            )

    out_accepted = sum(1 for r in outbound if r.get("accepted"))
    out_sent = sum(1 for r in outbound if r.get("sent"))
    out_delivered = sum(1 for r in outbound if r.get("delivered"))
    if args.phase2_only or args.phase2_verify_only:
        phase1_pass = (
            len(outbound) == 20 and sum(1 for r in outbound if r.get("delivered")) == 20
        )
    else:
        phase1_pass = (
            len(outbound) == 20
            and out_accepted == 20
            and out_sent == 20
            and out_delivered == 20
            and not adb_disconnects
            and all(r.get("send_success") for r in outbound)
        )

    durations = [r["duration_seconds"] for r in outbound if r.get("duration_seconds") is not None]
    delivery_range = {
        "min_seconds": min(durations) if durations else None,
        "max_seconds": max(durations) if durations else None,
        "phase1_wall_seconds": round(phase1_end - phase1_start, 3),
    }

    phase2_results: list[dict] = []
    phase2_pass = False
    run_phase2 = (
        args.phase2_only
        or args.phase2_verify_only
        or args.phase2
        or (args.execute and not args.phase2_only)
    )
    if run_phase2 and phase1_pass:
        vf_to_slot = {str(s["voidfix_device_id"]): int(s["slot"]) for s in slots}
        if service._delivery_poller is not None:
            phase2_results = run_phase2_inbound(
                plan,
                service._delivery_poller,
                vf_to_slot,
                send=not args.phase2_verify_only,
            )
        else:
            phase2_results = [{"error": "no delivery poller"}]
        phase2_pass = (
            len(phase2_results) == 20
            and all(r.get("received_once") for r in phase2_results)
            and all(r.get("received") for r in phase2_results)
            and all(r.get("matched_slot") for r in phase2_results)
        )
    elif run_phase2 and not phase1_pass:
        phase2_results = [{"skipped": True, "reason": "Phase 1 did not pass; no inbound per policy"}]

    all_records = outbox.list_records()
    fullbox_records = [r for r in all_records if r.idempotency_key.startswith(f"fullbox-{run_id}")]
    terminal = {}
    for r in fullbox_records:
        terminal[r.status.value] = terminal.get(r.status.value, 0) + 1

    failed_slots = [
        r.get("slot_id") or r.get("slot")
        for r in outbound
        if not r.get("delivered") or not r.get("send_success")
    ]
    failed_slots += [
        r["slot"] for r in phase2_results if isinstance(r.get("slot"), int) and not r.get("received_once")
    ]

    report = {
        "generated_at_utc": _ts(),
        "run_id": run_id,
        "overall_pass": phase1_pass and (phase2_pass if run_phase2 and phase1_pass else False),
        "phase1_outbound": {
            "pass": phase1_pass,
            "accepted": f"{out_accepted}/20",
            "sent": f"{out_sent}/20",
            "delivered": f"{out_delivered}/20",
            "delivery_times": delivery_range,
            "slots": outbound,
        },
        "phase2_inbound": {
            "pass": phase2_pass,
            "received": f"{sum(1 for r in phase2_results if r.get('received_once'))}/20",
            "slots": phase2_results,
        },
        "adb_disconnects": adb_disconnects,
        "voidfix_api_errors": [r for r in outbound if r.get("error") or r.get("send_error")],
        "duplicate_or_routing": [
            r for r in phase2_results if r.get("match_count", 0) != 1
        ],
        "outbox": {
            "fullbox_record_count": len(fullbox_records),
            "terminal_status_counts": terminal,
            "total_outbox_rows": len(all_records),
        },
        "failed_or_problem_slots": sorted(set(failed_slots)),
        "fully_validated_ready": phase1_pass and phase2_pass,
        "constraints": {
            "no_mapping_changes": True,
            "no_allowlist_changes": True,
            "no_daemon_restart": True,
            "no_raw_voidfix_scripts": True,
            "prototype_excluded": True,
        },
    }

    out_path = ROOT / "_tmp_fullbox_sms_simultaneous_report.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    FINAL_REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"overall_pass": report["overall_pass"], "wrote": str(FINAL_REPORT.name)}, indent=2))
    return 0 if report["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
