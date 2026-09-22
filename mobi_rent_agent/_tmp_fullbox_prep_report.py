"""Read-only full-box validation preparation (no SMS sends)."""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import requests
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from application.sms_factory import build_sms_dispatch_service, sms_dispatch_runtime_flags
from domain.farm import PROTOTYPE_VOIDFIX_DEVICE_ID, is_terminal_sms_final_status
from infrastructure.config import load_config
from infrastructure.redact import normalize_msisdn
from infrastructure.voidfix_devices import load_voidfix_device_map, load_voidfix_sim_slots
from infrastructure.voidfix_delivery import VoidFixDeliveryPoller, DEFAULT_READ_MESSAGES_ENDPOINT
from main import build_heartbeat_service, load_slot_map, SLOT_MAP_PATH

OUTBOX_PATH = ROOT / "logs" / "sms_outbox.sqlite"
PROTOTYPE_VF = PROTOTYPE_VOIDFIX_DEVICE_ID


def adb_state(serial: str) -> str:
    try:
        return subprocess.run(
            ["adb", "-s", serial, "get-state"],
            capture_output=True,
            text=True,
            timeout=12,
        ).stdout.strip()
    except subprocess.TimeoutExpired:
        return "timeout"


def ro_serial(serial: str) -> str:
    r = subprocess.run(
        ["adb", "-s", serial, "shell", "getprop", "ro.serialno"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return (r.stdout or "").strip()


def parse_api_sim2(raw: str) -> str | None:
    if not raw:
        return None
    m = re.search(r"(\+?\d{10,15})", raw)
    if not m:
        return None
    d = normalize_msisdn(m.group(1))
    return f"+{d}" if d else None


def fetch_api(key: str) -> dict[str, dict]:
    r = requests.post(
        "https://sms.voidfix.com/services/get-devices.php",
        data={"key": key},
        timeout=30,
    )
    r.raise_for_status()
    out: dict[str, dict] = {}
    for d in (r.json().get("data") or {}).get("devices") or []:
        did = str(d.get("id"))
        if did == PROTOTYPE_VF:
            continue
        sims = d.get("sims") or {}
        sim2_raw = str(sims.get("1") or "")
        out[did] = {"sim2_raw": sim2_raw, "sim2_phone": parse_api_sim2(sim2_raw)}
    return out


def main() -> int:
    config = load_config(str(ROOT / ".env"))
    slot_map = load_slot_map(config.slot_map_path or SLOT_MAP_PATH)
    vf_map = load_voidfix_device_map(
        config.voidfix_device_map_path or str(ROOT / "voidfix_devices.json")
    )
    sim_slots = load_voidfix_sim_slots(
        config.voidfix_device_map_path or str(ROOT / "voidfix_devices.json")
    )
    key = (dotenv_values(ROOT / ".env").get("VOIDFIX_API_KEY") or "").strip()

    # ADB + mapping rows
    participants: list[dict] = []
    adb_ok = 0
    map_ok = 0
    api_ok = 0
    for slot in range(1, 21):
        serial = slot_map.get(slot, "")
        vf = vf_map.get(slot, "")
        sim_slot = sim_slots.get(slot)
        st = adb_state(serial) if serial else "missing"
        ro = ro_serial(serial) if st == "device" else ""
        adb_pass = st == "device" and ro == serial
        if adb_pass:
            adb_ok += 1
        map_pass = bool(serial and vf and sim_slot == 1 and slot in config.voidfix_allowed_slot_ids)
        if map_pass:
            map_ok += 1
        participants.append(
            {
                "slot": slot,
                "adb_serial": serial,
                "ro_serialno": ro or None,
                "voidfix_id": vf,
                "sim_slot_send": sim_slot,
                "adb_online": adb_pass,
                "config_mapping_ok": map_pass,
            }
        )

    api_all = fetch_api(key) if key else {}
    vf_dup: dict[str, list[int]] = {}
    phone_dup: dict[str, list[int]] = {}
    for slot in range(1, 21):
        vf = vf_map.get(slot, "")
        phone = (api_all.get(vf) or {}).get("sim2_phone")
        participants[slot - 1]["sim2_msisdn_api"] = phone
        participants[slot - 1]["api_device_present"] = vf in api_all
        if vf and vf in api_all:
            api_ok += 1
        if vf:
            vf_dup.setdefault(vf, []).append(slot)
        if phone:
            p = normalize_msisdn(phone)
            phone_dup.setdefault(p, []).append(slot)
        participants[slot - 1]["full_chain_ok"] = (
            participants[slot - 1]["adb_online"]
            and participants[slot - 1]["config_mapping_ok"]
            and participants[slot - 1]["api_device_present"]
            and bool(phone)
        )

    vf_dup_bad = {k: v for k, v in vf_dup.items() if len(v) > 1}
    phone_dup_bad = {k: v for k, v in phone_dup.items() if len(v) > 1}

    # Heartbeat/API ping (one cycle, no SMS)
    hb = build_heartbeat_service(config, slot_map)
    hb_results = hb.run_once()
    hb_ok = sum(1 for r in hb_results if r.success)
    hb_by_slot = {r.slot_id: r for r in hb_results}

    # SMS dispatch composition (does not send)
    flags = sms_dispatch_runtime_flags(config)
    service = build_sms_dispatch_service(config)
    dispatch_ok = service is not None

    env_checks = {
        "voidfix_enabled": config.voidfix_enabled,
        "voidfix_dry_run": config.voidfix_dry_run,
        "voidfix_live_send_authorized": config.voidfix_live_send_authorized,
        "voidfix_sim_slot_send_enabled": config.voidfix_sim_slot_send_enabled,
        "voidfix_delivery_poll_enabled": config.voidfix_delivery_poll_enabled,
        "allowed_slots": sorted(config.voidfix_allowed_slot_ids),
        "recipient_allowlist": list(config.voidfix_recipient_allowlist or []),
        "read_messages_endpoint": config.voidfix_read_messages_endpoint,
        "prototype_vf_in_prod_map": PROTOTYPE_VF in set(vf_map.values()),
    }

    poller = VoidFixDeliveryPoller(
        key,
        read_messages_endpoint=config.voidfix_read_messages_endpoint or DEFAULT_READ_MESSAGES_ENDPOINT,
        timeout_seconds=config.request_timeout_seconds,
    )
    poller_uses_read_messages = "read-messages.php" in (poller._endpoint or "").lower()
    delivery_delivered_date_field = True  # voidfix_delivery.is_delivered uses deliveredDate

    # Outbox
    outbox_rows = 0
    non_terminal: list[dict] = []
    outbox_schema_ok = False
    if OUTBOX_PATH.is_file():
        conn = sqlite3.connect(OUTBOX_PATH)
        try:
            conn.execute("SELECT 1 FROM outbound_messages LIMIT 1")
            outbox_schema_ok = True
            rows = conn.execute("SELECT idempotency_key, slot_id, status, final_status, provider_message_id FROM outbound_messages").fetchall()
            outbox_rows = len(rows)
            for row in rows:
                fs = row[3]
                if not is_terminal_sms_final_status(fs):
                    non_terminal.append(
                        {
                            "idempotency_key": row[0],
                            "slot_id": row[1],
                            "status": row[2],
                            "final_status": fs,
                            "provider_message_id": row[4],
                        }
                    )
        finally:
            conn.close()
    else:
        outbox_schema_ok = True  # will be created on first send; empty is OK

    # Processes
    main_pids: list[int] = []
    ps = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'main\\.py' } | Select-Object -ExpandProperty ProcessId"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    for line in (ps.stdout or "").splitlines():
        line = line.strip()
        if line.isdigit():
            main_pids.append(int(line))

    issues: list[str] = []
    if adb_ok != 20:
        issues.append(f"ADB online {adb_ok}/20")
    if map_ok != 20:
        issues.append(f"config mapping {map_ok}/20")
    if api_ok != 20:
        issues.append(f"VoidFix API device entries {api_ok}/20")
    if sum(1 for p in participants if p["full_chain_ok"]) != 20:
        issues.append("not all slots pass full chain (ADB+config+API+MSISDN)")
    if vf_dup_bad:
        issues.append(f"duplicate voidfix ids: {vf_dup_bad}")
    if phone_dup_bad:
        issues.append(f"duplicate msisdn: {phone_dup_bad}")
    if hb_ok != 20:
        issues.append(f"heartbeat success {hb_ok}/20")
    if not dispatch_ok:
        issues.append("SmsDispatchService failed to construct")
    if env_checks["prototype_vf_in_prod_map"]:
        issues.append("prototype 1385 in production map")
    if PROTOTYPE_VF in api_all and vf_map.values():
        pass
    if len(main_pids) > 2:
        issues.append(f"multiple main.py PIDs ({len(main_pids)}): {main_pids}")
    if non_terminal:
        issues.append(f"non-terminal outbox rows: {len(non_terminal)}")
    if not poller_uses_read_messages:
        issues.append("delivery poller not using read-messages.php")
    if env_checks["voidfix_dry_run"]:
        issues.append("VOIDFIX_DRY_RUN is true")

    report = {
        "preparation_only": True,
        "no_sms_sent": True,
        "summary": {
            "adb_online": f"{adb_ok}/20",
            "config_mapping": f"{map_ok}/20",
            "voidfix_api_devices": f"{api_ok}/20",
            "full_chain_mapping": f"{sum(1 for p in participants if p['full_chain_ok'])}/20",
            "heartbeat_api": f"{hb_ok}/20",
            "duplicate_vf_ids": "none" if not vf_dup_bad else vf_dup_bad,
            "duplicate_msisdn": "none" if not phone_dup_bad else phone_dup_bad,
        },
        "sms_dispatch_readiness": {
            "dispatch_service_constructed": dispatch_ok,
            "flags": flags,
            "env": env_checks,
            "daemon_auto_send_on_startup": False,
            "note": "main.py validates dispatch and may resume poll-only outbox recovery; does not auto-send new SMS",
        },
        "delivery_polling_readiness": {
            "poller_active": flags.get("delivery_poller_active"),
            "endpoint": config.voidfix_read_messages_endpoint,
            "uses_read_messages_php": poller_uses_read_messages,
            "delivered_confirmation_field": "deliveredDate",
        },
        "durable_outbox_readiness": {
            "path": str(OUTBOX_PATH),
            "exists": OUTBOX_PATH.is_file(),
            "schema_ok": outbox_schema_ok,
            "total_rows": outbox_rows,
            "non_terminal_rows": len(non_terminal),
            "non_terminal_detail": non_terminal[:20],
            "ready_for_20_simultaneous": outbox_schema_ok and len(non_terminal) == 0,
        },
        "daemon_processes": {
            "main_py_pids": main_pids,
            "expected": "1 launcher + 1 child (2 PIDs) on Windows",
            "duplicate_daemon_risk": len(main_pids) > 2,
        },
        "test_plan_participants": participants,
        "heartbeat_slot_17": {
            "success": hb_by_slot.get(17).success if hb_by_slot.get(17) else None,
            "status_code": hb_by_slot.get(17).status_code if hb_by_slot.get(17) else None,
        },
        "blockers_before_20_phone_test": issues,
        "ready_for_simultaneous_test": len(issues) == 0,
    }

    out_json = ROOT / "_tmp_fullbox_prep_report.json"
    out_md = ROOT / "_tmp_fullbox_prep_test_plan.md"
    out_json.write_text(json.dumps(report, indent=2), encoding="utf-8")

    lines = [
        "# Full-box SMS validation — test plan (preparation only)",
        "",
        "Do **not** send until Marcus authorizes the simultaneous 20-phone test.",
        "",
        "| Slot | ADB serial | VoidFix | SIM 2 MSISDN (API) | ADB | Chain OK |",
        "|------|------------|---------|-------------------|-----|----------|",
    ]
    for p in participants:
        lines.append(
            f"| {p['slot']} | `{p['adb_serial']}` | {p['voidfix_id']} | {p.get('sim2_msisdn_api') or 'MISSING'} | "
            f"{'yes' if p['adb_online'] else 'NO'} | {'yes' if p['full_chain_ok'] else 'NO'} |"
        )
    lines.extend(["", f"**Blockers:** {issues or 'None'}", ""])
    out_md.write_text("\n".join(lines), encoding="utf-8")

    print(json.dumps(report["summary"], indent=2))
    print("ready_for_simultaneous_test", report["ready_for_simultaneous_test"])
    print("blockers", issues)
    print("Wrote", out_json, out_md)
    return 0 if report["ready_for_simultaneous_test"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
