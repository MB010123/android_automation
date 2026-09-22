"""Strict read-only verification slots 1-20: serial, VoidFix drawer ID, SIM2, API."""
from __future__ import annotations

import json
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from dotenv import dotenv_values

from infrastructure.redact import normalize_msisdn

ROOT = Path(__file__).resolve().parent
SLOT_MAP_PATH = ROOT / "slot_map.json"
PROTOTYPE_SERIAL = "3C071JEHN14705"
PROTOTYPE_VF = "1385"
SIMS_INTENT = ["am", "start", "-W", "-a", "android.settings.MANAGE_ALL_SIM_PROFILES_SETTINGS"]
VF_START = ["am", "start", "-W", "-n", "org.voidfix.smsgateway/.ui.StartActivity"]

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
    "1413": None,
    "1414": "+17634388552",
    "1415": "+16126499751",
    "1417": "+17633935255",
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


def adb_state(serial: str) -> str:
    return subprocess.run(
        ["adb", "-s", serial, "get-state"], capture_output=True, text=True, timeout=15
    ).stdout.strip()


def dump_root(serial: str) -> ET.Element | None:
    sh(serial, "uiautomator", "dump", "/data/local/tmp/vf_sv.xml")
    xml, _ = sh(serial, "cat", "/data/local/tmp/vf_sv.xml")
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


def voidfix_drawer_ids(serial: str) -> tuple[list[str], list[str]]:
    sh(serial, "input", "keyevent", "KEYCODE_HOME")
    time.sleep(0.4)
    subprocess.run(["adb", "-s", serial, "shell", *VF_START], timeout=35)
    time.sleep(2)
    root = dump_root(serial)
    if root is None:
        return [], []
    if any((n.attrib.get("text") or "") == "Set as default SMS app" for n in root.iter("node")):
        tap_text(root, serial, "NO")
        time.sleep(1)
        root = dump_root(serial)
    if root is None:
        return [], []
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
    time.sleep(1.2)
    root2 = dump_root(serial)
    labels: list[str] = []
    ids: list[str] = []
    if root2 is not None:
        for node in root2.iter("node"):
            t = (node.attrib.get("text") or "").strip()
            if re.search(r"\[\d{3,5}\]", t):
                labels.append(t)
        ids = sorted(set(re.findall(r"\[(\d{3,5})\]", "\n".join(labels))))
    sh(serial, "input", "keyevent", "KEYCODE_HOME")
    return labels, ids


def settings_sim2_phone(serial: str) -> str | None:
    subprocess.run(["adb", "-s", serial, "shell", *SIMS_INTENT], timeout=30)
    time.sleep(2.5)
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
            "model": d.get("model"),
            "sim2_raw": sim2_raw,
            "sim2_phone": parse_api_sim2(sim2_raw),
        }
    return out


def classify(row: dict) -> str:
    if row.get("skip"):
        return "SKIP"
    if row.get("adb") != "device":
        return "UNRESOLVED"
    if not row.get("serial_map_ok"):
        return "MISMATCH"
    if not row.get("ro_serial_ok"):
        return "MISMATCH"
    vf_ids = row.get("vf_ids") or []
    if len(vf_ids) != 1:
        return "UNRESOLVED" if not vf_ids else "MISMATCH"
    if not row.get("sim2_slot_ok"):
        return "MISMATCH"
    vf = vf_ids[0]
    api = row.get("api_for_vf") or {}
    if vf not in (row.get("api_all") or {}):
        return "MISMATCH"
    if vf == "1413" and row.get("phone") is None:
        return "UNRESOLVED"
    if row.get("phone") is None:
        return "UNRESOLVED"
    if api.get("sim2_phone") is None and vf != "1413":
        return "UNRESOLVED"
    if vf == "1413":
        if row.get("phone") is not None:
            return "MISMATCH"
        return "UNRESOLVED"
    if normalize_msisdn(row["phone"]) != normalize_msisdn(api["sim2_phone"]):
        return "MISMATCH"
    return "VERIFIED"


def main() -> None:
    key = (dotenv_values(ROOT / "config/prototype/prototype.env").get("VOIDFIX_API_KEY") or "").strip()
    if not key:
        raise SystemExit("missing VOIDFIX_API_KEY")
    slot_map = json.loads(SLOT_MAP_PATH.read_text(encoding="utf-8"))
    api_all = fetch_api(key)

    rows: list[dict] = []
    for slot in range(1, 21):
        slot_s = str(slot)
        mapped = slot_map.get(slot_s, "").strip()
        row: dict = {
            "slot": slot,
            "adb_serial_map": mapped,
            "adb": adb_state(mapped) if mapped else "missing",
        }
        if mapped == PROTOTYPE_SERIAL:
            row["skip"] = True
            row["result"] = "SKIP"
            rows.append(row)
            continue
        if row["adb"] != "device":
            row["result"] = "UNRESOLVED"
            row["note"] = "adb offline or missing"
            rows.append(row)
            continue

        ro, _ = sh(mapped, "getprop", "ro.serialno")
        ro = ro.strip()
        row["ro_serialno"] = ro
        row["serial_map_ok"] = bool(mapped) and mapped == ro
        row["ro_serial_ok"] = row["serial_map_ok"]

        labels, vf_ids = voidfix_drawer_ids(mapped)
        row["vf_drawer_labels"] = labels
        row["vf_ids"] = vf_ids
        row["vf_id"] = vf_ids[0] if len(vf_ids) == 1 else None

        row["sim2_slot_ok"] = isub_sim2_ok(mapped)
        row["phone"] = settings_sim2_phone(mapped)
        sh(mapped, "input", "keyevent", "KEYCODE_HOME")

        if row["vf_id"]:
            row["api_for_vf"] = api_all.get(row["vf_id"])
        row["api_all"] = api_all

        ref = REFERENCE.get(row["vf_id"]) if row.get("vf_id") else None
        row["reference_phone"] = ref

        api_phone = (row.get("api_for_vf") or {}).get("sim2_phone")
        if row.get("vf_id") and api_phone and row.get("phone"):
            row["api_match"] = normalize_msisdn(row["phone"]) == normalize_msisdn(api_phone)
        elif row.get("vf_id") == "1413" and row.get("phone") is None:
            row["api_match"] = (row.get("api_for_vf") or {}).get("sim2_raw", "").find("US Mobile") >= 0
        else:
            row["api_match"] = False

        row["result"] = classify(row)
        rows.append(row)

    # Duplicates
    vf_to_slots: dict[str, list[int]] = {}
    phone_to_slots: dict[str, list[int]] = {}
    for r in rows:
        if r.get("vf_id"):
            vf_to_slots.setdefault(r["vf_id"], []).append(r["slot"])
        if r.get("phone"):
            phone_to_slots.setdefault(normalize_msisdn(r["phone"]), []).append(r["slot"])

    assigned_vf = {r["vf_id"] for r in rows if r.get("vf_id")}
    unassigned_api = sorted(
        did for did in api_all if did not in assigned_vf and did not in (PROTOTYPE_VF,)
    )

    report = {
        "rows": rows,
        "duplicates_vf_id": {k: v for k, v in vf_to_slots.items() if len(v) > 1},
        "duplicates_phone": {k: v for k, v in phone_to_slots.items() if len(v) > 1},
        "unassigned_api_devices": unassigned_api,
        "vf_1411_on_farm": [r for r in rows if r.get("vf_id") == "1411" or normalize_msisdn(r.get("phone") or "") == "16126499573"],
    }

    out_json = ROOT / "_tmp_strict_slot_verify.json"
    out_json.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    def fmt_phone(p: str | None) -> str:
        return p if p else "(none)"

    print("Slot | ADB Serial | VoidFix ID | SIM Slot | Phone Number | API Match | Result")
    print("---")
    for r in rows:
        if r.get("skip"):
            continue
        vf = r.get("vf_id") or ("MULTI:" + ",".join(r.get("vf_ids") or []) if r.get("vf_ids") else "?")
        sim = "SIM2" if r.get("sim2_slot_ok") else "SIM2?"
        ph = fmt_phone(r.get("phone"))
        am = "yes" if r.get("api_match") else "no"
        serial = r.get("ro_serialno") or r.get("adb_serial_map") or "?"
        print(f"{r['slot']} | {serial} | {vf} | {sim} | {ph} | {am} | {r.get('result')}")

    for title, key in [
        ("VERIFIED", "VERIFIED"),
        ("UNRESOLVED", "UNRESOLVED"),
        ("MISMATCH", "MISMATCH"),
    ]:
        print(f"\n## {title}")
        for r in rows:
            if r.get("result") == key:
                print(f"  slot {r['slot']}: {r.get('note') or ''} vf={r.get('vf_id')} phone={fmt_phone(r.get('phone'))}")

    print("\n## DUPLICATES")
    print("  vf_id:", report["duplicates_vf_id"] or "(none)")
    print("  phone:", report["duplicates_phone"] or "(none)")

    print("\n## UNASSIGNED API DEVICES")
    print(" ", unassigned_api or "(none)")

    print("\n## VF 1411 / +16126499573 on farm")
    hits = report["vf_1411_on_farm"]
    print(" ", hits if hits else "(not found on slots 1-20)")

    print("\nWrote", out_json)


if __name__ == "__main__":
    main()
