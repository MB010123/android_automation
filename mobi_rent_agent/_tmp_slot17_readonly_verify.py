"""Read-only Slot 17 verification before production enablement."""
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
from infrastructure.voidfix_devices import load_voidfix_device_map

SLOT = 17
EXPECTED_SERIAL = "25061FDF6006KF"
EXPECTED_VF = "1413"
PROTOTYPE_VF = "1385"
PROD_SLOTS = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 18, 19, 20)
VF_START = ["am", "start", "-W", "-n", "org.voidfix.smsgateway/.ui.StartActivity"]
SIMS_INTENT = ["am", "start", "-W", "-a", "android.settings.MANAGE_ALL_SIM_PROFILES_SETTINGS"]

# Authoritative bay map: slot_map.json + verified drawer ID (strict verify 2026-09-18).
# MSISDN for 1413 comes from VoidFix API at verify time (US Mobile provisioning).


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


def dump_root(serial: str) -> ET.Element | None:
    sh(serial, "uiautomator", "dump", "/data/local/tmp/s17_vf.xml")
    xml, _ = sh(serial, "cat", "/data/local/tmp/s17_vf.xml")
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


def settings_sim_info(serial: str) -> tuple[str | None, list[str]]:
    subprocess.run(["adb", "-s", serial, "shell", *SIMS_INTENT], timeout=25)
    time.sleep(2.0)
    root = dump_root(serial)
    labels: list[str] = []
    phone: str | None = None
    if root is not None:
        for node in root.iter("node"):
            t = (node.attrib.get("text") or "").strip()
            if t:
                labels.append(t)
            if re.search(r"\d{3}.*\d{4}", t):
                digits = normalize_msisdn(t)
                if len(digits) >= 10:
                    phone = f"+{digits}"
    sh(serial, "input", "keyevent", "KEYCODE_HOME")
    return phone, labels


def isub_detail(serial: str) -> tuple[bool, str]:
    blob, _ = sh(serial, "dumpsys", "isub")
    ok = "simSlotIndex=1" in blob
    us_mobile = "US Mobile" in blob or "Dark Star" in blob
    snippet = ""
    for line in blob.splitlines():
        if "simSlotIndex=1" in line or "US Mobile" in line or "isEmbedded=1" in line:
            snippet += line.strip()[:200] + "\n"
    return ok and us_mobile, snippet[:1200]


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
    slot_map = json.loads((ROOT / "slot_map.json").read_text(encoding="utf-8"))
    config = load_config(str(ROOT / ".env"))
    device_map = load_voidfix_device_map(
        config.voidfix_device_map_path or str(ROOT / "voidfix_devices.json")
    )
    key = (dotenv_values(ROOT / ".env").get("VOIDFIX_API_KEY") or "").strip()
    api_all = fetch_api(key) if key else {}

    mapped = slot_map.get(str(SLOT), "").strip()
    expected_phone = (api_all.get(EXPECTED_VF) or {}).get("sim2_phone")
    expected_raw = (api_all.get(EXPECTED_VF) or {}).get("sim2_raw")

    state = subprocess.run(
        ["adb", "-s", EXPECTED_SERIAL, "get-state"],
        capture_output=True,
        text=True,
        timeout=12,
    ).stdout.strip()

    ro = ""
    drawer_ids: list[str] = []
    settings_phone: str | None = None
    settings_labels: list[str] = []
    isub_ok = False
    isub_snip = ""
    if state == "device":
        ro, _ = sh(EXPECTED_SERIAL, "getprop", "ro.serialno")
        ro = ro.strip()
        drawer_ids = voidfix_drawer_ids(EXPECTED_SERIAL)
        settings_phone, settings_labels = settings_sim_info(EXPECTED_SERIAL)
        isub_ok, isub_snip = isub_detail(EXPECTED_SERIAL)

    vf_dup_slots = [s for s, vf in device_map.items() if vf == EXPECTED_VF]
    phone_dup_slots: list[int] = []
    if expected_phone:
        d = normalize_msisdn(expected_phone)
        for s, vf in device_map.items():
            p = normalize_msisdn((api_all.get(vf) or {}).get("sim2_phone") or "")
            if p and p == d:
                phone_dup_slots.append(s)

    checks = {
        "slot_map_serial": mapped == EXPECTED_SERIAL,
        "adb_online": state == "device",
        "ro_serial_match": ro == EXPECTED_SERIAL,
        "voidfix_drawer_1413": drawer_ids == [EXPECTED_VF],
        "isub_sim2_us_mobile": isub_ok,
        "settings_has_us_mobile": any("US Mobile" in x for x in settings_labels),
        "settings_msisdn_present": settings_phone is not None,
        "settings_msisdn_matches_api": (
            normalize_msisdn(settings_phone or "") == normalize_msisdn(expected_phone or "")
            if settings_phone and expected_phone
            else None
        ),
        "api_has_e164_for_1413": expected_phone is not None,
        "vf_not_in_prod_map_yet": SLOT not in device_map,
        "vf_id_not_duplicated_prod": len(vf_dup_slots) == 0,
        "msisdn_not_duplicated_prod": len(phone_dup_slots) == 0,
    }

    report = {
        "readonly": True,
        "no_sms": True,
        "authoritative_map": {
            "slot": SLOT,
            "adb_serial": EXPECTED_SERIAL,
            "voidfix_id": EXPECTED_VF,
            "sim_slot_send_param": 1,
            "expected_msisdn_from_api": expected_phone,
            "expected_api_sim2_raw": expected_raw,
        },
        "live": {
            "adb_state": state,
            "ro_serialno": ro,
            "voidfix_drawer_ids": drawer_ids,
            "settings_sim2_msisdn": settings_phone,
            "settings_labels_sample": [x for x in settings_labels if "US" in x or re.search(r"\\d", x)][:12],
            "isub_snippet": isub_snip,
        },
        "duplicate_scan": {
            "vf_1413_prod_slots": vf_dup_slots,
            "msisdn_prod_slots": phone_dup_slots,
        },
        "checks": checks,
        "ready_for_prod_config": all(
            checks[k]
            for k in (
                "slot_map_serial",
                "adb_online",
                "ro_serial_match",
                "voidfix_drawer_1413",
                "isub_sim2_us_mobile",
                "settings_msisdn_present",
                "settings_msisdn_matches_api",
                "api_has_e164_for_1413",
                "vf_not_in_prod_map_yet",
                "vf_id_not_duplicated_prod",
                "msisdn_not_duplicated_prod",
            )
            if checks[k] is not False
        )
        and checks.get("settings_msisdn_matches_api") is not False,
    }
    # ready only if msisdn match is True not None
    if checks.get("settings_msisdn_matches_api") is not True:
        report["ready_for_prod_config"] = False

    out = ROOT / "_tmp_slot17_readonly_report.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["ready_for_prod_config"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
