"""Read-only health check for 19 verified production VoidFix slots."""
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

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from infrastructure.config import load_config
from infrastructure.redact import normalize_msisdn
from infrastructure.voidfix_devices import load_voidfix_device_map, load_voidfix_sim_slots

TARGET_SLOTS = tuple(range(1, 21))
PROTOTYPE_VF = "1385"
VF_START = ["am", "start", "-W", "-n", "org.voidfix.smsgateway/.ui.StartActivity"]
SIMS_INTENT = ["am", "start", "-W", "-a", "android.settings.MANAGE_ALL_SIM_PROFILES_SETTINGS"]

REFERENCE: dict[str, str | None] = {
    "1386": "+19522287088",
    "1389": "+16514722709",
    "1393": "+17633287165",
    "1394": "+17633468225",
    "1395": "+16126499401",
    "1402": "+17633390175",
    "1403": "+16126499481",
    "1404": "+17633573736",
    "1405": "+16126499603",
    "1406": "+17633406158",
    "1407": "+16126499651",
    "1408": "+17634382664",
    "1409": "+16126499683",
    "1410": "+17633935186",
    "1411": "+16126499573",
    "1412": "+17634384448",
    "1413": "+17633887556",
    "1414": "+17634388552",
    "1415": "+16126499751",
    "1417": "+17633935255",
}


def sh(serial: str, *args: str, timeout: float = 40) -> tuple[str, int]:
    r = subprocess.run(
        ["adb", "-s", serial, "shell", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return (r.stdout or "") + (r.stderr or ""), r.returncode


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


def dump_root(serial: str) -> ET.Element | None:
    sh(serial, "uiautomator", "dump", "/data/local/tmp/vf_hc.xml")
    xml, _ = sh(serial, "cat", "/data/local/tmp/vf_hc.xml")
    try:
        return ET.fromstring(xml)
    except ET.ParseError:
        return None


def tap_bounds(serial: str, bounds: str) -> None:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
    if not m:
        return
    x1, y1, x2, y2 = map(int, m.groups())
    sh(serial, "input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2))


def tap_text(root: ET.Element, serial: str, text: str) -> bool:
    for node in root.iter("node"):
        if (node.attrib.get("text") or "").strip() == text:
            b = node.attrib.get("bounds") or ""
            if b:
                tap_bounds(serial, b)
                return True
    return False


def voidfix_drawer_id(serial: str) -> str | None:
    sh(serial, "input", "keyevent", "KEYCODE_HOME")
    time.sleep(0.35)
    subprocess.run(["adb", "-s", serial, "shell", *VF_START], timeout=30)
    time.sleep(1.8)
    root = dump_root(serial)
    if root is None:
        return None
    if any((n.attrib.get("text") or "") == "Set as default SMS app" for n in root.iter("node")):
        tap_text(root, serial, "NO")
        time.sleep(0.8)
        root = dump_root(serial)
    if root is None:
        return None
    opened = False
    for node in root.iter("node"):
        desc = (node.attrib.get("content-desc") or "").lower()
        if "open navigation drawer" in desc:
            b = node.attrib.get("bounds") or ""
            if b:
                tap_bounds(serial, b)
                opened = True
                break
    if not opened:
        sh(serial, "input", "tap", "80", "180")
    time.sleep(1.0)
    root2 = dump_root(serial)
    ids: list[str] = []
    if root2 is not None:
        labels = [(n.attrib.get("text") or "").strip() for n in root2.iter("node")]
        ids = sorted(set(re.findall(r"\[(\d{3,5})\]", "\n".join(labels))))
    sh(serial, "input", "keyevent", "KEYCODE_HOME")
    if len(ids) == 1:
        return ids[0]
    return None


def settings_sim2_phone(serial: str) -> str | None:
    subprocess.run(["adb", "-s", serial, "shell", *SIMS_INTENT], timeout=25)
    time.sleep(2.0)
    root = dump_root(serial)
    if root is None:
        return None
    for node in root.iter("node"):
        t = (node.attrib.get("text") or "").strip()
        if re.search(r"\d{3}.*\d{4}", t):
            digits = normalize_msisdn(t)
            if len(digits) >= 10:
                return f"+{digits}"
    return None


def isub_sim2_ok(serial: str) -> bool:
    blob, _ = sh(serial, "dumpsys", "isub")
    return "simSlotIndex=1" in blob


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
        out[did] = {
            "sim2_raw": sim2_raw,
            "sim2_phone": parse_api_sim2(sim2_raw),
        }
    return out


def pf(ok: bool | None) -> str:
    if ok is None:
        return "UNRESOLVED"
    return "PASS" if ok else "FAIL"


def main() -> int:
    config = load_config(str(ROOT / ".env"))
    allowed = set(config.voidfix_allowed_slot_ids)
    vf_map_path = config.voidfix_device_map_path or str(ROOT / "voidfix_devices.json")
    device_map = load_voidfix_device_map(vf_map_path)
    sim_slots = load_voidfix_sim_slots(vf_map_path)
    slot_map = json.loads((ROOT / "slot_map.json").read_text(encoding="utf-8"))

    key = (dotenv_values(ROOT / ".env").get("VOIDFIX_API_KEY") or "").strip()
    if not key:
        print("FAIL: VOIDFIX_API_KEY missing in .env")
        return 1
    api_all = fetch_api(key)

    rows: list[dict] = []
    for slot in TARGET_SLOTS:
        mapped = slot_map.get(str(slot), "").strip()
        expected_vf = device_map.get(slot)
        expected_phone = REFERENCE.get(expected_vf or "")
        sim_slot_cfg = sim_slots.get(slot)

        adb = adb_state(mapped) if mapped else "missing"
        adb_online = adb == "device"

        ro = ""
        map_match: bool | None = None
        live_vf: str | None = None
        sim2_ok: bool | None = None
        live_phone: str | None = None
        api_phone: str | None = None

        if not mapped:
            map_match = False
        elif not adb_online:
            map_match = None
        else:
            ro, _ = sh(mapped, "getprop", "ro.serialno")
            ro = ro.strip()
            map_match = ro == mapped
            sim2_ok = isub_sim2_ok(mapped)
            live_vf = voidfix_drawer_id(mapped)
            live_phone = settings_sim2_phone(mapped)
            sh(mapped, "input", "keyevent", "KEYCODE_HOME")

        if expected_vf:
            api_phone = (api_all.get(expected_vf) or {}).get("sim2_phone")

        in_vf_json = slot in device_map
        in_allow = slot in allowed
        simslot_ok = sim_slot_cfg == 1

        vf_match = None
        if expected_vf and live_vf:
            vf_match = live_vf == expected_vf
        elif expected_vf and not adb_online:
            vf_match = None
        elif expected_vf:
            vf_match = False

        phone_match = None
        if expected_phone and live_phone:
            phone_match = normalize_msisdn(live_phone) == normalize_msisdn(expected_phone)
        elif expected_phone and api_phone and not live_phone:
            phone_match = normalize_msisdn(api_phone) == normalize_msisdn(expected_phone)
        elif expected_phone and not adb_online:
            phone_match = None
        elif expected_phone:
            phone_match = False

        dup = "PASS"
        rows.append(
            {
                "slot": slot,
                "adb_serial": ro or mapped or "?",
                "voidfix_id": expected_vf or "?",
                "live_voidfix_id": live_vf,
                "sim2_number": live_phone or api_phone or expected_phone or "",
                "simslot": sim_slot_cfg,
                "adb_online": pf(adb_online if mapped else False),
                "map_match": pf(map_match),
                "vf_match": pf(vf_match),
                "sim2_active": pf(sim2_ok),
                "simslot_cfg": pf(simslot_ok if in_vf_json else False),
                "phone_match": pf(phone_match),
                "in_vf_json": pf(in_vf_json),
                "in_allowlist": pf(in_allow),
                "dup_placeholder": dup,
            }
        )

    # Global duplicate checks
    vf_ids = [r["voidfix_id"] for r in rows if r["voidfix_id"] != "?"]
    vf_dup = len(vf_ids) != len(set(vf_ids))
    phones = [
        normalize_msisdn(r["sim2_number"])
        for r in rows
        if r["sim2_number"] and normalize_msisdn(r["sim2_number"])
    ]
    phone_dup = len(phones) != len(set(phones))

    vf_to_slots: dict[str, list[int]] = {}
    for r in rows:
        vf_to_slots.setdefault(r["voidfix_id"], []).append(r["slot"])
    vf_dup_map = {k: v for k, v in vf_to_slots.items() if len(v) > 1}

    phone_to_slots: dict[str, list[int]] = {}
    for r in rows:
        p = normalize_msisdn(r["sim2_number"])
        if p:
            phone_to_slots.setdefault(p, []).append(r["slot"])
    phone_dup_map = {k: v for k, v in phone_to_slots.items() if len(v) > 1}

    for r in rows:
        dup_fail = (
            vf_dup_map.get(r["voidfix_id"])
            or phone_dup_map.get(normalize_msisdn(r["sim2_number"]) or "")
        )
        r["duplicate_check"] = "FAIL" if dup_fail else "PASS"

    def overall(r: dict) -> str:
        if r["adb_online"] == "UNRESOLVED":
            return "UNRESOLVED"
        checks = [
            r["adb_online"],
            r["map_match"],
            r["vf_match"],
            r["sim2_active"],
            r["simslot_cfg"],
            r["phone_match"],
            r["in_vf_json"],
            r["in_allowlist"],
            r["duplicate_check"],
        ]
        if "FAIL" in checks:
            return "FAIL"
        if "UNRESOLVED" in checks:
            return "UNRESOLVED"
        return "PASS"

    for r in rows:
        r["overall"] = overall(r)

    slot15_row = next((r for r in rows if r["slot"] == 15), None)
    slot17_row = next((r for r in rows if r["slot"] == 17), None)
    slot15_vf_ok = (
        slot15_row is not None
        and slot15_row.get("voidfix_id") == "1411"
        and slot15_row.get("live_voidfix_id") == "1411"
    )
    slot17_vf_ok = (
        slot17_row is not None
        and slot17_row.get("voidfix_id") == "1413"
        and slot17_row.get("live_voidfix_id") == "1413"
    )
    slot15_phone_ok = slot15_row is not None and slot15_row.get("phone_match") == "PASS"
    slot17_phone_ok = slot17_row is not None and slot17_row.get("phone_match") == "PASS"
    prototype_isolated = PROTOTYPE_VF not in [r["voidfix_id"] for r in rows]

    report = {
        "rows": rows,
        "vf_duplicate_map": vf_dup_map,
        "phone_duplicate_map": phone_dup_map,
        "slot_15_in_production": 15 in allowed and 15 in device_map,
        "slot_17_in_production": 17 in allowed and 17 in device_map,
        "slot_15_vf_1411": slot15_vf_ok,
        "slot_17_vf_1413": slot17_vf_ok,
        "slot_15_msisdn": slot15_phone_ok,
        "slot_17_msisdn": slot17_phone_ok,
        "prototype_1385_isolated": prototype_isolated,
        "expected_slot_count": 20,
        "configured_slot_count": len(rows),
    }
    out = ROOT / "_tmp_prod_20_health_report.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(
        "Slot | ADB Serial | VoidFix ID | SIM 2 Number | simSlot | "
        "ADB Online | Map Match | Duplicate Check | Overall"
    )
    print("---")
    for r in rows:
        num = r["sim2_number"] or "UNRESOLVED"
        if num.startswith("+") and len(num) > 6:
            num = num[:2] + "XXX" + num[-4:]
        print(
            f"{r['slot']} | {r['adb_serial']} | {r['voidfix_id']} | {num} | "
            f"{r['simslot']} | {r['adb_online']} | {r['map_match']} | "
            f"{r['duplicate_check']} | {r['overall']}"
        )

    totals = {"PASS": 0, "FAIL": 0, "UNRESOLVED": 0}
    for r in rows:
        totals[r["overall"]] = totals.get(r["overall"], 0) + 1

    print("\n## SUMMARY")
    print("PASS", totals.get("PASS", 0))
    print("FAIL", totals.get("FAIL", 0))
    print("UNRESOLVED", totals.get("UNRESOLVED", 0))
    print("slot_15_in_production", report["slot_15_in_production"])
    print("slot_17_in_production", report["slot_17_in_production"])
    print("slot_15_vf_1411", report["slot_15_vf_1411"])
    print("slot_17_vf_1413", report["slot_17_vf_1413"])
    print("slot_15_msisdn", report["slot_15_msisdn"])
    print("slot_17_msisdn", report["slot_17_msisdn"])
    print("prototype_1385_isolated", report["prototype_1385_isolated"])
    print("vf_dup", vf_dup_map or "(none)")
    print("phone_dup", phone_dup_map or "(none)")
    print("Wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
