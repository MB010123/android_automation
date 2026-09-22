"""Read-only verification for PhoneFarm Slot 15 (no SMS, no config edits)."""
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

SLOT = 15
EXPECTED_SERIAL = "21051FDF600EM9"
EXPECTED_VF = "1411"
EXPECTED_PHONE = "+16126499573"
PROTOTYPE_VF = "1385"
VF_START = ["am", "start", "-W", "-n", "org.voidfix.smsgateway/.ui.StartActivity"]
SIMS_INTENT = ["am", "start", "-W", "-a", "android.settings.MANAGE_ALL_SIM_PROFILES_SETTINGS"]


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
    sh(serial, "uiautomator", "dump", "/data/local/tmp/s15_vf.xml")
    xml, _ = sh(serial, "cat", "/data/local/tmp/s15_vf.xml")
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


def voidfix_drawer_ids(serial: str) -> list[str]:
    sh(serial, "input", "keyevent", "KEYCODE_HOME")
    time.sleep(0.35)
    subprocess.run(["adb", "-s", serial, "shell", *VF_START], timeout=30)
    time.sleep(1.8)
    root = dump_root(serial)
    if root is None:
        return []
    if any((n.attrib.get("text") or "") == "Set as default SMS app" for n in root.iter("node")):
        tap_text(root, serial, "NO")
        time.sleep(0.8)
        root = dump_root(serial)
    if root is None:
        return []
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
    return ids


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
    sh(serial, "input", "keyevent", "KEYCODE_HOME")
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
        out[did] = {"sim2_raw": sim2_raw, "sim2_phone": parse_api_sim2(sim2_raw)}
    return out


def main() -> int:
    config = load_config(str(ROOT / ".env"))
    slot_map = json.loads((ROOT / "slot_map.json").read_text(encoding="utf-8"))
    vf_path = config.voidfix_device_map_path or str(ROOT / "voidfix_devices.json")
    device_map = load_voidfix_device_map(vf_path)
    sim_slots = load_voidfix_sim_slots(vf_path)
    allowed = set(config.voidfix_allowed_slot_ids)

    key = (dotenv_values(ROOT / ".env").get("VOIDFIX_API_KEY") or "").strip()
    api_all = fetch_api(key) if key else {}

    mapped_serial = slot_map.get(str(SLOT), "").strip()
    serial_map_ok = mapped_serial == EXPECTED_SERIAL

    adb = adb_state(mapped_serial) if mapped_serial else "missing"
    adb_online = adb == "device"

    slot15: dict = {
        "slot": SLOT,
        "authoritative_adb_serial": mapped_serial,
        "expected_adb_serial": EXPECTED_SERIAL,
        "serial_map_match": serial_map_ok,
        "adb_state": adb,
        "adb_online": adb_online,
    }

    if adb_online:
        ro, _ = sh(mapped_serial, "getprop", "ro.serialno")
        slot15["ro_serialno"] = ro.strip()
        slot15["ro_serial_matches_map"] = ro.strip() == mapped_serial
        slot15["live_voidfix_drawer_ids"] = voidfix_drawer_ids(mapped_serial)
        slot15["live_voidfix_id"] = (
            slot15["live_voidfix_drawer_ids"][0]
            if len(slot15["live_voidfix_drawer_ids"]) == 1
            else None
        )
        slot15["sim2_present_active_isub"] = isub_sim2_ok(mapped_serial)
        slot15["sim2_phone_settings"] = settings_sim2_phone(mapped_serial)
    else:
        slot15["live_voidfix_id"] = None
        slot15["sim2_phone_settings"] = None
        slot15["sim2_present_active_isub"] = None

    api_1411 = api_all.get(EXPECTED_VF) or {}
    slot15["voidfix_api_1411_sim2"] = api_1411.get("sim2_phone")
    slot15["voidfix_api_1411_raw"] = api_1411.get("sim2_raw")
    slot15["expected_voidfix_id"] = EXPECTED_VF
    slot15["expected_sim2_phone"] = EXPECTED_PHONE
    slot15["simslot_send_param"] = 1  # VoidFix API key "1" = SIM 2 on farm

    # Config posture (read-only)
    vf_id_in_other_slot = {s: vf for s, vf in device_map.items() if vf == EXPECTED_VF}
    slot15["in_voidfix_devices_json"] = SLOT in device_map
    slot15["in_voidfix_allowlist"] = SLOT in allowed
    slot15["voidfix_1411_assigned_in_json"] = vf_id_in_other_slot

    # Farm scan: VF 1411 or MSISDN on any online handset
    farm_hits: list[dict] = []
    target_digits = normalize_msisdn(EXPECTED_PHONE)
    for bay, serial in sorted(slot_map.items(), key=lambda x: int(x[0])):
        if adb_state(serial) != "device":
            continue
        ids = voidfix_drawer_ids(serial)
        phone = settings_sim2_phone(serial)
        sh(serial, "input", "keyevent", "KEYCODE_HOME")
        hit_vf = EXPECTED_VF in ids
        hit_phone = phone and normalize_msisdn(phone) == target_digits
        if hit_vf or hit_phone:
            farm_hits.append(
                {
                    "slot": int(bay),
                    "serial": serial,
                    "drawer_vf_ids": ids,
                    "sim2_phone_settings": phone,
                    "hit_vf_1411": hit_vf,
                    "hit_phone_1411_ref": hit_phone,
                }
            )

    # Duplicate MSISDN vs 18 production slots
    prod_phones: dict[str, list[int]] = {}
    for s, vf in device_map.items():
        ref = (api_all.get(vf) or {}).get("sim2_phone") or ""
        d = normalize_msisdn(ref)
        if d:
            prod_phones.setdefault(d, []).append(s)
    dup_prod = prod_phones.get(target_digits, [])

    report = {
        "readonly": True,
        "no_sms": True,
        "slot15": slot15,
        "vf_1411_on_other_slots": farm_hits,
        "expected_phone_on_prod_slots_via_api": dup_prod,
        "slot17_excluded_from_prod": 17 not in allowed and 17 not in device_map,
        "adb_farm_count": len(
            [
                l
                for l in subprocess.run(
                    ["adb", "devices"], capture_output=True, text=True
                ).stdout.splitlines()
                if "\tdevice" in l
            ]
        ),
        "missing_adb_serials_vs_slot_map": sorted(
            set(slot_map.values())
            - {
                l.split("\t")[0].strip()
                for l in subprocess.run(
                    ["adb", "devices"], capture_output=True, text=True
                ).stdout.splitlines()
                if "\tdevice" in l
            }
        ),
    }

    out_path = ROOT / "_tmp_slot15_readonly_report.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if adb_online else 2


if __name__ == "__main__":
    raise SystemExit(main())
