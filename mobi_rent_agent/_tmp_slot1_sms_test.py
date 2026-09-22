"""One controlled Slot 1 SMS test — precheck, send, poll. No second send."""
from __future__ import annotations

import json
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import dotenv_values

from infrastructure.redact import normalize_msisdn
from infrastructure.voidfix_api import interpret_send_response

ROOT = Path(__file__).resolve().parent
SERIAL = "18171FDF6005WG"
VF_ID = "1386"
TO = "+19522287088"
MESSAGE = "Mobi-Rent Slot 1 SMS TEST"
SEND_URL = "https://sms.voidfix.com/services/send.php"
DEVICES_URL = "https://sms.voidfix.com/services/get-devices.php"
# Common VoidFix companion endpoints (best-effort poll)
POLL_URLS = [
    "https://sms.voidfix.com/services/get-messages.php",
    "https://sms.voidfix.com/services/get-sent-messages.php",
    "https://sms.voidfix.com/services/messages.php",
]


def ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def sh(serial: str, *args: str) -> str:
    r = subprocess.run(
        ["adb", "-s", serial, "shell", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
    )
    return (r.stdout or "") + (r.stderr or "")


def dump_texts(serial: str) -> list[str]:
    sh(serial, "uiautomator", "dump", "/data/local/tmp/s1_sms_ui.xml")
    xml = sh(serial, "cat", "/data/local/tmp/s1_sms_ui.xml")
    out: list[str] = []
    try:
        for node in ET.fromstring(xml).iter("node"):
            for a in ("text", "content-desc"):
                v = (node.attrib.get(a) or "").strip()
                if v:
                    out.append(v)
    except ET.ParseError:
        pass
    return out


def tap_text(serial: str, text: str) -> bool:
    sh(serial, "uiautomator", "dump", "/data/local/tmp/s1_sms_tap.xml")
    xml = sh(serial, "cat", "/data/local/tmp/s1_sms_tap.xml")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return False
    for node in root.iter("node"):
        if (node.attrib.get("text") or "").strip() == text:
            b = node.attrib.get("bounds") or ""
            m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
            if m:
                x1, y1, x2, y2 = map(int, m.groups())
                sh(serial, "input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2))
                return True
    return False


def voidfix_drawer_id(serial: str) -> list[str]:
    sh(serial, "input", "keyevent", "KEYCODE_HOME")
    subprocess.run(
        ["adb", "-s", serial, "shell", "am", "start", "-W", "-n", "org.voidfix.smsgateway/.ui.StartActivity"],
        timeout=35,
    )
    time.sleep(2)
    texts0 = dump_texts(serial)
    if "Set as default SMS app" in texts0:
        tap_text(serial, "NO")
        time.sleep(0.8)
    sh(serial, "uiautomator", "dump", "/data/local/tmp/s1_sms_dr.xml")
    xml = sh(serial, "cat", "/data/local/tmp/s1_sms_dr.xml")
    opened = False
    try:
        for node in ET.fromstring(xml).iter("node"):
            d = (node.attrib.get("content-desc") or "").lower()
            if "open navigation drawer" in d:
                b = node.attrib.get("bounds") or ""
                m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
                if m:
                    x1, y1, x2, y2 = map(int, m.groups())
                    sh(serial, "input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2))
                    opened = True
                break
    except ET.ParseError:
        pass
    if not opened:
        sh(serial, "input", "tap", "80", "180")
    time.sleep(1.2)
    texts2 = dump_texts(serial)
    ids = sorted(set(re.findall(r"\[(\d{3,5})\]", "\n".join(texts2))))
    sh(serial, "input", "keyevent", "KEYCODE_HOME")
    return ids


def settings_sim2_phone(serial: str) -> str | None:
    subprocess.run(
        [
            "adb",
            "-s",
            serial,
            "shell",
            "am",
            "start",
            "-W",
            "-a",
            "android.settings.MANAGE_ALL_SIM_PROFILES_SETTINGS",
        ],
        timeout=30,
    )
    time.sleep(2.5)
    for t in dump_texts(serial):
        if re.search(r"\d{3}.*\d{4}", t):
            d = normalize_msisdn(t)
            if len(d) >= 10:
                return f"+{d}"
    return None


def voidfix_dashboard_counts(serial: str) -> dict[str, str]:
    subprocess.run(
        ["adb", "-s", serial, "shell", "am", "start", "-W", "-n", "org.voidfix.smsgateway/.ui.StartActivity"],
        timeout=35,
    )
    time.sleep(2)
    texts = dump_texts(serial)
    counts: dict[str, str] = {}
    for label in ("Pending", "Sent", "Delivered", "Failed"):
        if label in texts:
            idx = texts.index(label)
            if idx > 0 and texts[idx - 1].isdigit():
                counts[label] = texts[idx - 1]
    sh(serial, "input", "keyevent", "KEYCODE_HOME")
    return counts


def messages_app_has_body(serial: str, needle: str) -> bool:
    subprocess.run(
        ["adb", "-s", serial, "shell", "am", "start", "-W", "-a", "android.intent.action.MAIN", "-c", "android.intent.category.APP_MESSAGING"],
        timeout=25,
    )
    time.sleep(3)
    blob = "\n".join(dump_texts(serial)).lower()
    sh(serial, "input", "keyevent", "KEYCODE_HOME")
    return needle.lower() in blob


def poll_message_status(session: requests.Session, key: str, msg_id: str) -> list[dict]:
    attempts: list[dict] = []
    for url in POLL_URLS:
        for extra in ({}, {"id": msg_id}, {"message_id": msg_id}, {"ID": msg_id}):
            try:
                r = session.post(url, data={"key": key, **extra}, timeout=20)
                attempts.append(
                    {
                        "at": ts(),
                        "url": url,
                        "extra_keys": list(extra.keys()),
                        "http": r.status_code,
                        "body_preview": r.text[:500],
                    }
                )
            except requests.RequestException as exc:
                attempts.append({"at": ts(), "url": url, "error": str(exc)})
    return attempts


def find_status_in_json(obj: object, msg_id: str) -> dict | None:
    if isinstance(obj, dict):
        if str(obj.get("ID") or obj.get("id") or "") == msg_id:
            return obj
        for v in obj.values():
            hit = find_status_in_json(v, msg_id)
            if hit:
                return hit
    elif isinstance(obj, list):
        for item in obj:
            hit = find_status_in_json(item, msg_id)
            if hit:
                return hit
    return None


def main() -> None:
    key = (dotenv_values(ROOT / "config/prototype/prototype.env").get("VOIDFIX_API_KEY") or "").strip()
    if not key:
        raise SystemExit("VOIDFIX_API_KEY missing")

    slot_map = json.loads((ROOT / "slot_map.json").read_text(encoding="utf-8"))
    report: dict = {"events": [], "prechecks": {}, "send": {}, "poll": [], "delivery": {}}

    def log(event: str, **data: object) -> None:
        report["events"].append({"at": ts(), "event": event, **data})

    mapped = slot_map.get("1", "")
    ro = sh(SERIAL, "getprop", "ro.serialno").strip()
    adb = subprocess.run(["adb", "-s", SERIAL, "get-state"], capture_output=True, text=True).stdout.strip()
    vf_ids = voidfix_drawer_id(SERIAL)
    phone = settings_sim2_phone(SERIAL)
    sh(SERIAL, "input", "keyevent", "KEYCODE_HOME")

    r_dev = requests.post(DEVICES_URL, data={"key": key}, timeout=25)
    api_body = r_dev.json()
    api_1386 = None
    for d in (api_body.get("data") or {}).get("devices") or []:
        if str(d.get("id")) == VF_ID:
            api_1386 = d
            break
    api_sim_raw = (api_1386 or {}).get("sims") or {}
    api_sim1 = str(api_sim_raw.get("1") or "")
    api_phone = None
    m_api = re.search(r"(\+?\d{10,15})", api_sim1)
    if m_api:
        api_phone = f"+{normalize_msisdn(m_api.group(1))}"

    report["prechecks"] = {
        "slot_map_serial": mapped,
        "ro_serialno": ro,
        "serial_match": mapped == ro == SERIAL,
        "adb_state": adb,
        "voidfix_drawer_ids": vf_ids,
        "settings_sim2_phone": phone,
        "api_device_1386_sim_key_1": api_sim1,
        "api_device_1386_phone": api_phone,
        "all_prechecks_pass": (
            mapped == SERIAL
            and ro == SERIAL
            and adb == "device"
            and vf_ids == [VF_ID]
            and phone == TO
            and api_phone == TO
        ),
    }
    log("prechecks_done", precheck_pass=report["prechecks"]["all_prechecks_pass"])

    if not report["prechecks"]["all_prechecks_pass"]:
        out = ROOT / "_tmp_slot1_sms_test_report.json"
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print("PRECHECK FAILED — not sending SMS")
        print(json.dumps(report["prechecks"], indent=2))
        print("Wrote", out)
        return

    counts_before = voidfix_dashboard_counts(SERIAL)
    log("dashboard_before", counts=counts_before)

    session = requests.Session()
    send_at = ts()
    form = {
        "key": key,
        "number": TO,
        "message": MESSAGE,
        "devices": VF_ID,
        "simSlot": "1",
    }
    response = session.post(SEND_URL, data=form, timeout=30)
    result = interpret_send_response(response, TO, secret=key)
    send_body = None
    try:
        send_body = response.json()
    except ValueError:
        send_body = {"raw": response.text[:1000]}

    report["send"] = {
        "at": send_at,
        "http_status": response.status_code,
        "success": result.success,
        "provider_message_id": result.provider_message_id,
        "outcome": result.outcome.value if result.outcome else None,
        "error": result.error,
        "response_redacted": json.loads(json.dumps(send_body).replace(key, "<redacted>")),
    }
    log("send_complete", message_id=result.provider_message_id)

    msg_id = result.provider_message_id or ""
    timeline: list[dict] = []
    if msg_id:
        timeline.append({"at": ts(), "status": "Pending", "source": "send_response", "raw": report["send"]["response_redacted"]})

    # Poll up to ~3 minutes
    deadline = time.time() + 180
    last_status = "Pending" if result.success else "Failed"
    while time.time() < deadline and result.success and msg_id:
        time.sleep(20)
        now = ts()
        counts = voidfix_dashboard_counts(SERIAL)
        timeline.append({"at": now, "voidfix_dashboard": counts})
        poll_hits = poll_message_status(session, key, msg_id)
        report["poll"].extend(poll_hits)
        for hit in poll_hits:
            if hit.get("http") != 200:
                continue
            try:
                body = json.loads(hit.get("body_preview", "{}"))
            except json.JSONDecodeError:
                continue
            row = find_status_in_json(body, msg_id)
            if row and row.get("status"):
                last_status = str(row["status"])
                timeline.append({"at": now, "status": last_status, "source": "api_poll", "row": row})
        if messages_app_has_body(SERIAL, MESSAGE):
            timeline.append({"at": now, "status": "Delivered", "source": "messages_app_ui"})
            last_status = "Delivered"
            break
        if last_status.lower() in ("delivered", "sent", "failed", "canceled"):
            if last_status.lower() in ("delivered", "failed", "canceled"):
                break

    counts_after = voidfix_dashboard_counts(SERIAL)
    report["delivery"] = {
        "timeline": timeline,
        "dashboard_before": counts_before,
        "dashboard_after": counts_after,
        "final_status": last_status,
        "messages_app_seen": messages_app_has_body(SERIAL, MESSAGE),
    }

    out = ROOT / "_tmp_slot1_sms_test_report.json"
    # redact key in full report
    text = json.dumps(report, indent=2, ensure_ascii=False).replace(key, "<redacted>")
    out.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
