"""Strict read-only identity verification for Slot 15 only."""
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

SERIAL = "21051FDF600EM9"
SLOT = 15
EXPECTED_VF = "1411"
EXPECTED_PHONE = "+16126499573"
PROTOTYPE_VF = "1385"
PROD_SLOTS = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 16, 18, 19, 20)
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


def dump_root(serial: str) -> ET.Element | None:
    sh(serial, "uiautomator", "dump", "/data/local/tmp/s15_strict.xml")
    xml, _ = sh(serial, "cat", "/data/local/tmp/s15_strict.xml")
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


def isub_sim2_active(serial: str) -> tuple[bool, str]:
    blob, _ = sh(serial, "dumpsys", "isub")
    has_index = "simSlotIndex=1" in blob
    # Heuristic: active data/voice sub on slot 1
    active_markers = [
        "mActiveDataSubId",
        "mActiveSubId",
        "simSlotIndex=1",
    ]
    snippet = ""
    for line in blob.splitlines():
        if "simSlotIndex=1" in line or "slotIndex=1" in line:
            snippet += line.strip() + "\n"
    ok = has_index
    return ok, snippet[:800]


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


def pf(ok: bool | None) -> str:
    if ok is None:
        return "UNAVAILABLE"
    return "PASS" if ok else "FAIL"


def main() -> int:
    config = load_config(str(ROOT / ".env"))
    vf_path = config.voidfix_device_map_path or str(ROOT / "voidfix_devices.json")
    device_map = load_voidfix_device_map(vf_path)
    sim_slots = load_voidfix_sim_slots(vf_path)
    allowed = set(config.voidfix_allowed_slot_ids)
    slot_map = json.loads((ROOT / "slot_map.json").read_text(encoding="utf-8"))

    key = (dotenv_values(ROOT / ".env").get("VOIDFIX_API_KEY") or "").strip()
    api_all = fetch_api(key) if key else {}

    state = subprocess.run(
        ["adb", "-s", SERIAL, "get-state"],
        capture_output=True,
        text=True,
        timeout=12,
    ).stdout.strip()
    ro, _ = sh(SERIAL, "getprop", "ro.serialno")
    ro = ro.strip()

    drawer_ids = voidfix_drawer_ids(SERIAL) if state == "device" else []
    live_vf = drawer_ids[0] if len(drawer_ids) == 1 else None
    isub_ok, isub_snip = isub_sim2_active(SERIAL) if state == "device" else (None, "")
    settings_phone = settings_sim2_phone(SERIAL) if state == "device" else None
    sh(SERIAL, "input", "keyevent", "KEYCODE_HOME")

    checks = {
        "adb_online": state == "device",
        "ro_serialno": ro,
        "ro_serial_match": ro == SERIAL,
        "voidfix_drawer_ids": drawer_ids,
        "voidfix_id_exact_1411": live_vf == EXPECTED_VF,
        "isub_sim2_simSlotIndex_1": isub_ok,
        "settings_sim2_msisdn": settings_phone,
        "settings_msisdn_match": (
            normalize_msisdn(settings_phone or "") == normalize_msisdn(EXPECTED_PHONE)
            if settings_phone
            else None
        ),
    }

    # Production duplicate scan (config + API for mapped VF IDs)
    vf_to_slots: dict[str, list[int]] = {}
    phone_to_slots: dict[str, list[int]] = {}
    prod_rows: list[dict] = []
    for slot in PROD_SLOTS:
        vf = device_map.get(slot, "")
        phone = (api_all.get(vf) or {}).get("sim2_phone") or ""
        prod_rows.append({"slot": slot, "voidfix_id": vf, "sim2_api": phone})
        if vf:
            vf_to_slots.setdefault(vf, []).append(slot)
        p = normalize_msisdn(phone)
        if p:
            phone_to_slots.setdefault(p, []).append(slot)

    vf_dup = {k: v for k, v in vf_to_slots.items() if len(v) > 1}
    phone_dup = {k: v for k, v in phone_to_slots.items() if len(v) > 1}
    vf_1411_in_prod = vf_to_slots.get(EXPECTED_VF, [])
    phone_1411_in_prod = phone_to_slots.get(normalize_msisdn(EXPECTED_PHONE), [])

    prod_config = {
        "slot15_in_voidfix_devices_json": SLOT in device_map,
        "slot15_in_allowlist": SLOT in allowed,
        "slot15_mapped_serial": slot_map.get(str(SLOT)),
        "voidfix_1411_assigned_prod_slots": vf_1411_in_prod,
        "msisdn_16126499573_on_prod_slots_api": phone_1411_in_prod,
        "prod_vf_id_duplicates": vf_dup,
        "prod_msisdn_duplicates": phone_dup,
    }

    overall_fail = any(
        checks[k] is False
        for k in (
            "adb_online",
            "ro_serial_match",
            "voidfix_id_exact_1411",
            "isub_sim2_simSlotIndex_1",
            "settings_msisdn_match",
        )
    ) or prod_config["slot15_in_voidfix_devices_json"] or prod_config["slot15_in_allowlist"]

    report = {
        "readonly": True,
        "slot": SLOT,
        "expected": {"serial": SERIAL, "voidfix_id": EXPECTED_VF, "sim2_msisdn": EXPECTED_PHONE},
        "checks": {k: (v if not isinstance(v, bool) else pf(v)) for k, v in checks.items()},
        "isub_snippet": isub_snip,
        "voidfix_api_1411": api_all.get(EXPECTED_VF),
        "production_config": prod_config,
        "production_duplicate_scan_pass": not vf_dup and not phone_dup and not vf_1411_in_prod and not phone_1411_in_prod,
        "overall": "FAIL" if overall_fail else "PASS",
    }

    out = ROOT / "_tmp_slot15_strict_verify_report.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["overall"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
