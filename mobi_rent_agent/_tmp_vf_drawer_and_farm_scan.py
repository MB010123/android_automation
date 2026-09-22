"""Read-only: VoidFix drawer menu on slot 1; farm scan for +16126499573 (ref 1411)."""
from __future__ import annotations

import json
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

SLOT_MAP = Path(__file__).with_name("slot_map.json")
SLOT1 = "18171FDF6005WG"
TARGET_SUFFIX = "6126499573"


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


def dump_texts(serial: str, remote: str) -> list[str]:
    sh(serial, "uiautomator", "dump", remote)
    xml = sh(serial, "cat", remote)
    out: list[str] = []
    try:
        for node in ET.fromstring(xml).iter("node"):
            for attr in ("text", "content-desc"):
                v = (node.attrib.get(attr) or "").strip()
                if v:
                    out.append(v)
    except ET.ParseError:
        pass
    return out


def sim_phone(serial: str) -> str | None:
    subprocess.run(
        ["adb", "-s", serial, "shell", "am", "start", "-W", "-a", "android.settings.MANAGE_ALL_SIM_PROFILES_SETTINGS"],
        timeout=25,
    )
    time.sleep(2.5)
    texts = dump_texts(serial, "/data/local/tmp/vf_scan_sim.xml")
    for t in texts:
        if re.search(r"\d{3}.*\d{4}", t):
            digits = re.sub(r"\D", "", t)
            if len(digits) >= 10:
                return f"+{digits}"
    return None


def drawer_probe(serial: str) -> dict:
    subprocess.run(
        ["adb", "-s", serial, "shell", "am", "start", "-W", "-n", "org.voidfix.smsgateway/.ui.StartActivity"],
        timeout=30,
    )
    time.sleep(2)
    main = dump_texts(serial, "/data/local/tmp/vf_dr_main.xml")
    # hamburger ~ top-left (Pixel-class)
    sh(serial, "input", "tap", "80", "180")
    time.sleep(1.2)
    drawer = dump_texts(serial, "/data/local/tmp/vf_dr_open.xml")
    ids = sorted(set(re.findall(r"\b(13\d{2}|14\d{2})\b", "\n".join(drawer + main))))
    sh(serial, "input", "keyevent", "KEYCODE_HOME")
    return {"main_unique": sorted(set(main)), "drawer_unique": sorted(set(drawer)), "numeric_ids": ids}


def main() -> None:
    report: dict = {}
    if subprocess.run(["adb", "-s", SLOT1, "get-state"], capture_output=True, text=True).stdout.strip() == "device":
        report["slot1_drawer"] = drawer_probe(SLOT1)
        report["slot1_notifications"] = sh(SLOT1, "dumpsys", "notification", "--noredact")[:8000]
        vf_notif = [
            ln
            for ln in report["slot1_notifications"].splitlines()
            if "voidfix" in ln.lower() or "org.voidfix" in ln
        ]
        report["slot1_notification_voidfix_lines"] = vf_notif[:30]

    slots = json.loads(SLOT_MAP.read_text(encoding="utf-8"))
    farm: list[dict] = []
    for slot, serial in sorted(slots.items(), key=lambda x: int(x[0])):
        if slot == "1385":
            continue
        if subprocess.run(["adb", "-s", serial, "get-state"], capture_output=True, text=True).stdout.strip() != "device":
            farm.append({"slot": int(slot), "serial": serial, "adb": "offline"})
            continue
        ph = sim_phone(serial)
        farm.append(
            {
                "slot": int(slot),
                "serial": serial,
                "phone": ph,
                "matches_1411_ref": ph is not None and TARGET_SUFFIX in re.sub(r"\D", "", ph),
            }
        )
        sh(serial, "input", "keyevent", "KEYCODE_HOME")

    report["farm_scan_1411_number"] = farm
    out = Path(__file__).with_name("_tmp_vf_drawer_farm_report.json")
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print("wrote", out)
    print("slot1 drawer items:", (report.get("slot1_drawer") or {}).get("drawer_unique"))
    print("1411 matches:", [r for r in farm if r.get("matches_1411_ref")])


if __name__ == "__main__":
    main()
