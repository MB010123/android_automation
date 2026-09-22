"""Read-only delivery investigation for VoidFix message 3933486 on Slot 1."""
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

ROOT = Path(__file__).resolve().parent
SERIAL = "18171FDF6005WG"
MSG_ID = "3933486"
NEEDLE = "Mobi-Rent Slot 1 SMS TEST"
VF = "1386"

BASE = "https://sms.voidfix.com/services"
ENDPOINTS = [
    "get-messages.php",
    "get-message.php",
    "get-sent-messages.php",
    "messages.php",
    "message-status.php",
    "get-outbox-messages.php",
    "read-messages.php",
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
        timeout=60,
    )
    return (r.stdout or "") + (r.stderr or "")


def dump_all_text(serial: str) -> str:
    sh(serial, "uiautomator", "dump", "/data/local/tmp/dlv.xml")
    xml = sh(serial, "cat", "/data/local/tmp/dlv.xml")
    parts: list[str] = []
    try:
        for node in ET.fromstring(xml).iter("node"):
            for a in ("text", "content-desc"):
                v = (node.attrib.get(a) or "").strip()
                if v:
                    parts.append(v)
    except ET.ParseError:
        parts.append(xml[:2000])
    return "\n".join(parts)


def tap_desc_or_text(serial: str, *needles: str) -> bool:
    sh(serial, "uiautomator", "dump", "/data/local/tmp/dlv_tap.xml")
    xml = sh(serial, "cat", "/data/local/tmp/dlv_tap.xml")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return False
    for node in root.iter("node"):
        t = (node.attrib.get("text") or "").strip()
        d = (node.attrib.get("content-desc") or "").strip()
        for n in needles:
            if n.lower() in t.lower() or n.lower() in d.lower():
                b = node.attrib.get("bounds") or ""
                m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
                if m:
                    x1, y1, x2, y2 = map(int, m.groups())
                    sh(serial, "input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2))
                    return True
    return False


def voidfix_ui_probe(serial: str) -> dict:
    out: dict = {}
    sh(serial, "input", "keyevent", "KEYCODE_HOME")
    subprocess.run(
        ["adb", "-s", serial, "shell", "am", "start", "-W", "-n", "org.voidfix.smsgateway/.ui.StartActivity"],
        timeout=35,
    )
    time.sleep(2)
    if "Set as default SMS app" in dump_all_text(serial):
        tap_desc_or_text(serial, "NO")
        time.sleep(1)
    dash = dump_all_text(serial)
    out["dashboard_text"] = dash
    counts = {}
    lines = dash.splitlines()
    for label in ("Pending", "Sent", "Delivered", "Failed"):
        if label in lines:
            i = lines.index(label)
            if i > 0 and lines[i - 1].isdigit():
                counts[label] = lines[i - 1]
    out["dashboard_counts"] = counts

    tap_desc_or_text(serial, "Open navigation drawer") or sh(serial, "input", "tap", "80", "180")
    time.sleep(1)
    drawer = dump_all_text(serial)
    out["drawer_has_messages"] = "Messages" in drawer
    tap_desc_or_text(serial, "Messages")
    time.sleep(2)
    msg_ui = dump_all_text(serial)
    out["messages_screen_sample"] = msg_ui[:4000]
    out["messages_screen_has_needle"] = NEEDLE in msg_ui
    out["messages_screen_has_msg_id"] = MSG_ID in msg_ui

    # scroll attempt
    sh(serial, "input", "swipe", "400", "1400", "400", "400", "400")
    time.sleep(1)
    msg_ui2 = dump_all_text(serial)
    out["messages_after_scroll_has_needle"] = NEEDLE in msg_ui2

    sh(serial, "input", "keyevent", "KEYCODE_HOME")
    return out


def messages_app_probe(serial: str) -> dict:
    out: dict = {}
    subprocess.run(
        [
            "adb",
            "-s",
            serial,
            "shell",
            "am",
            "start",
            "-W",
            "-n",
            "com.google.android.apps.messaging/.ui.ConversationListActivity",
        ],
        timeout=30,
    )
    time.sleep(3)
    t1 = dump_all_text(serial)
    out["conversation_list_sample"] = t1[:5000]
    out["list_has_needle"] = NEEDLE in t1
    out["list_has_87088"] = "87088" in t1 or "952-228-7088" in t1 or "(952) 228-7088" in t1

    # open first conversation mentioning 7088 if present
    if tap_desc_or_text(serial, "7088", "87088", "952"):
        time.sleep(2)
        t2 = dump_all_text(serial)
        out["thread_sample"] = t2[:5000]
        out["thread_has_needle"] = NEEDLE in t2
    sh(serial, "input", "keyevent", "KEYCODE_HOME")
    return out


def local_traces(serial: str) -> dict:
    notif = sh(serial, "dumpsys", "notification", "--noredact")
    vf_notif = [ln for ln in notif.splitlines() if "voidfix" in ln.lower()][:20]
    log = sh(serial, "logcat", "-d", "-t", "400")
    hits = [
        ln
        for ln in log.splitlines()
        if NEEDLE.lower() in ln.lower() or MSG_ID in ln or "3933486" in ln
    ][:30]
    vf_log = [ln for ln in log.splitlines() if "voidfix" in ln.lower()][:15]
    return {
        "voidfix_notification_lines": vf_notif,
        "logcat_needle_or_id": hits,
        "logcat_voidfix_tail": vf_log,
    }


def try_http(session: requests.Session, method: str, url: str, data: dict) -> dict:
    try:
        if method == "GET":
            r = session.get(url, params=data, timeout=25)
        else:
            r = session.post(url, data=data, timeout=25)
    except requests.RequestException as exc:
        return {"error": str(exc)}
    body = r.text
    parsed = None
    try:
        parsed = r.json()
    except ValueError:
        parsed = None
    return {
        "http": r.status_code,
        "content_type": r.headers.get("Content-Type"),
        "len": len(body),
        "text_head": body[:800] if body else "",
        "json": parsed,
    }


def find_message(obj: object, msg_id: str) -> list[dict]:
    found: list[dict] = []

    def walk(x: object) -> None:
        if isinstance(x, dict):
            id_val = str(x.get("ID") or x.get("id") or x.get("message_id") or "")
            if id_val == msg_id:
                found.append(x)
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for i in x:
                walk(i)

    walk(obj)
    return found


def probe_api(key: str) -> dict:
    session = requests.Session()
    attempts: list[dict] = []
    param_sets = [
        {"key": key},
        {"key": key, "ID": MSG_ID},
        {"key": key, "id": MSG_ID},
        {"key": key, "message_id": MSG_ID},
        {"key": key, "messageID": MSG_ID},
        {"key": key, "devices": VF},
        {"key": key, "deviceID": VF},
        {"key": key, "device": VF},
        {"key": key, "type": "sent"},
        {"key": key, "type": "outbox"},
        {"key": key, "status": "all"},
        {"key": key, "ID": MSG_ID, "devices": VF},
    ]
    hits: list[dict] = []
    for ep in ENDPOINTS:
        url = f"{BASE}/{ep}"
        for params in param_sets:
            for method in ("POST", "GET"):
                res = try_http(session, method, url, params)
                row = {
                    "endpoint": ep,
                    "method": method,
                    "params": [k for k in params if k != "key"],
                    "http": res.get("http"),
                    "len": res.get("len"),
                    "content_type": res.get("content_type"),
                }
                if res.get("json") is not None:
                    row["json_success"] = (res["json"] or {}).get("success") if isinstance(res["json"], dict) else None
                    row["matches"] = find_message(res["json"], MSG_ID)
                    if row["matches"]:
                        hits.extend(row["matches"])
                elif res.get("len", 0) > 0:
                    row["text_head"] = res.get("text_head")
                attempts.append(row)
                # stop early on first rich JSON for get-messages
                if ep == "get-messages.php" and method == "POST" and params == {"key": key} and res.get("len", 0) > 0:
                    row["full_json"] = res.get("json")

    # dedupe hits
    uniq = {json.dumps(h, sort_keys=True): h for h in hits}
    return {"attempts_summary": attempts, "message_rows": list(uniq.values())}


def main() -> None:
    key = (dotenv_values(ROOT / "config/prototype/prototype.env").get("VOIDFIX_API_KEY") or "").strip()
    report = {
        "at": ts(),
        "message_id": MSG_ID,
        "repo_notes": {
            "production_polling": "SmsDispatchService has no delivery poll; CONFIRMED = send accepted with provider_message_id",
            "voidfix_api": "interpret_send_response only; no get-messages in voidfix_api.py",
            "readme": "delivery-report callbacks unsupported; VOIDFIX_INBOUND_ENDPOINT for inbound only",
            "tmp_script_bug": "_tmp_slot1_sms_test.py stored body_preview[:500] but empty body means len=0 not parse failure",
        },
        "api_probe": probe_api(key),
        "handset_voidfix": voidfix_ui_probe(SERIAL),
        "handset_messages": messages_app_probe(SERIAL),
        "handset_traces": local_traces(SERIAL),
        "known_send_snapshot": json.loads(
            (ROOT / "_tmp_slot1_sms_test_report.json").read_text(encoding="utf-8")
        ).get("send", {}),
    }
    out = ROOT / "_tmp_slot1_delivery_investigate.json"
    text = json.dumps(report, indent=2, ensure_ascii=False).replace(key, "<redacted>")
    out.write_text(text, encoding="utf-8")
    # concise stdout
    rows = report["api_probe"]["message_rows"]
    print("message_rows_from_api", len(rows))
    for r in rows[:3]:
        print(" ", {k: r.get(k) for k in ("ID", "id", "status", "deliveredDate", "sentDate", "message")})
    print("voidfix_counts", report["handset_voidfix"].get("dashboard_counts"))
    print("vf_messages_needle", report["handset_voidfix"].get("messages_screen_has_needle"))
    print("gmsg_needle", report["handset_messages"].get("list_has_needle"), report["handset_messages"].get("thread_has_needle"))
    print("wrote", out)


if __name__ == "__main__":
    main()
