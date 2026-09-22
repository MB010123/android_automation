"""Read-only VoidFix device ID investigation. Slot 1 deep; exception slots light."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parent))

VF = "org.voidfix.smsgateway"
VF_MAIN = "org.voidfix.smsgateway/.ui.StartActivity"
SIMS = "am start -W -a android.settings.MANAGE_ALL_SIM_PROFILES_SETTINGS"

SLOT1 = "18171FDF6005WG"
EXCEPTIONS = {
    11: "1C071FDF6004MS",
    17: "25061FDF6006KF",
    19: "1C101FDF6009EZ",
}


def sh(serial: str, *args: str, timeout: float = 45) -> tuple[str, int]:
    r = subprocess.run(
        ["adb", "-s", serial, "shell", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return (r.stdout or "") + (r.stderr or ""), r.returncode


def grep_hits(blob: str, label: str) -> list[str]:
    hits = []
    patterns = [
        r"device[_ ]?id",
        r"deviceId",
        r"deviceID",
        r"voidfix",
        r"1386",
        r"account",
        r"user[_ ]?id",
        r"firebase",
        r"installation",
        r"token",
    ]
    for i, line in enumerate(blob.splitlines()):
        if any(re.search(p, line, re.I) for p in patterns):
            if len(line) > 400:
                line = line[:400] + "..."
            hits.append(f"{label}:{i+1}:{line.strip()}")
    return hits[:40]


def ui_all_text(serial: str) -> str:
    subprocess.run(["adb", "-s", serial, "shell", f"am start -W -n {VF_MAIN}"], timeout=30)
    time.sleep(2)
    sh(serial, "uiautomator", "dump", "/data/local/tmp/vf_id_ui.xml")
    xml = sh(serial, "cat", "/data/local/tmp/vf_id_ui.xml")[0]
    texts = []
    try:
        for node in ET.fromstring(xml).iter("node"):
            t = node.attrib.get("text") or ""
            d = node.attrib.get("content-desc") or ""
            if t.strip():
                texts.append(t.strip())
            if d.strip():
                texts.append(f"[desc]{d.strip()}")
    except ET.ParseError:
        pass
    return "\n".join(texts), xml


def sim_phone(serial: str) -> str | None:
    subprocess.run(["adb", "-s", serial, "shell", SIMS], timeout=25)
    time.sleep(2.5)
    sh(serial, "uiautomator", "dump", "/data/local/tmp/vf_ex_sim.xml")
    xml = sh(serial, "cat", "/data/local/tmp/vf_ex_sim.xml")[0]
    for node in ET.fromstring(xml).iter("node"):
        t = (node.attrib.get("text") or "").strip()
        if re.search(r"\d{3}.*\d{4}", t):
            digits = re.sub(r"\D", "", t)
            if len(digits) >= 10:
                return f"+{digits}" if not t.startswith("+") else t
    return None


def investigate_serial(serial: str, deep: bool) -> dict:
    out: dict = {"serial": serial, "findings": []}
    if subprocess.run(["adb", "-s", serial, "get-state"], capture_output=True, text=True).stdout.strip() != "device":
        out["findings"].append("ADB not device")
        return out

    probes = [
        ("dumpsys_package", ["dumpsys", "package", VF]),
        ("dumpsys_activity", ["dumpsys", "activity", "service", VF]),
        ("dumpsys_activity_all", ["dumpsys", "activity", "activities"]),
        ("pm_dump", ["pm", "dump", VF]),
    ]
    if deep:
        probes += [
            ("logcat_voidfix", ["logcat", "-d", "-t", "300"]),
            ("settings_secure", ["settings", "list", "secure"]),
            ("settings_global", ["settings", "list", "global"]),
            ("account", ["dumpsys", "account"]),
        ]

    for name, args in probes:
        blob, rc = sh(serial, *args)
        if name == "logcat_voidfix":
            blob = "\n".join(l for l in blob.splitlines() if "voidfix" in l.lower() or VF in l)
        if name == "dumpsys_activity_all":
            blob = "\n".join(l for l in blob.splitlines() if VF in l or "voidfix" in l.lower())
        hits = grep_hits(blob, name)
        if hits:
            out["findings"].extend(hits)

    runas, rc = sh(serial, "run-as", VF, "ls", "shared_prefs")
    out["run_as_shared_prefs"] = f"rc={rc} head={runas[:200]!r}"

    su, rc = sh(serial, "su", "-c", f"ls /data/data/{VF}/shared_prefs")
    out["su_shared_prefs"] = f"rc={rc} head={su[:200]!r}"

    ext, _ = sh(serial, "ls", "-la", f"/sdcard/Android/data/{VF}")
    out["external_data"] = ext[:300]

    texts, _xml = ui_all_text(serial)
    out["vf_ui_text_sample"] = texts[:2500]
    id_in_ui = re.findall(r"\b(13\d{2}|14\d{2})\b", texts)
    out["vf_ui_numeric_ids"] = sorted(set(id_in_ui))

    sh(serial, "input", "keyevent", "KEYCODE_HOME")
    return out


def api_devices(key: str) -> dict:
    r = requests.post("https://sms.voidfix.com/services/get-devices.php", data={"key": key}, timeout=25)
    rows = {}
    for d in (r.json().get("data") or {}).get("devices") or []:
        did = str(d.get("id"))
        if did == "1385":
            continue
        sims = d.get("sims") or {}
        rows[did] = sims
    return rows


def main() -> None:
    report_path = Path(__file__).with_name("_tmp_vf_id_investigation_report.json")
    key = (dotenv_values("config/prototype/prototype.env").get("VOIDFIX_API_KEY") or "").strip()
    report: dict = {"slot1_deep": None, "api": {}, "exceptions": []}

    report["slot1_deep"] = investigate_serial(SLOT1, deep=True)

    api = api_devices(key)
    for did in ["1407", "1411", "1413", "1415"]:
        report["api"][did] = api.get(did, "MISSING")

    for slot, serial in EXCEPTIONS.items():
        row: dict = {"slot": slot, "serial": serial}
        if subprocess.run(
            ["adb", "-s", serial, "get-state"], capture_output=True, text=True
        ).stdout.strip() != "device":
            row["adb"] = "not device"
            report["exceptions"].append(row)
            continue
        row["phone_settings"] = sim_phone(serial)
        isub = sh(serial, "dumpsys", "isub")[0]
        row["isub_sim2_present"] = "simSlotIndex=1" in isub
        row["isub_us_mobile_embedded"] = "isEmbedded=1" in isub and "US Mobile" in isub
        ex = investigate_serial(serial, deep=False)
        row["vf_ui_numeric_ids"] = ex.get("vf_ui_numeric_ids")
        row["probe_hits_count"] = len(ex.get("findings", []))
        report["exceptions"].append(row)

    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    s1 = report["slot1_deep"] or {}
    print("Wrote", report_path)
    print("run-as:", s1.get("run_as_shared_prefs"))
    print("UI numeric ids:", s1.get("vf_ui_numeric_ids"))
    print("Probe hits count:", len(s1.get("findings", [])))
    for h in s1.get("findings", [])[:30]:
        print(" ", h.encode("ascii", "backslashreplace").decode())
    print("API:", report["api"])
    for row in report["exceptions"]:
        print(row)
    print("VERIFIED none - handset VF ID chain incomplete")


if __name__ == "__main__":
    main()
