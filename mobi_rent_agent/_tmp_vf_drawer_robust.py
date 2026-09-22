"""Open VoidFix nav drawer via content-desc bounds; parse device [ID] labels."""
from __future__ import annotations

import json
import re
import subprocess
import time
import xml.etree.ElementTree as ET

SERIALS = {
    11: "1C071FDF6004MS",
    17: "25061FDF6006KF",
    19: "1C101FDF6009EZ",
}


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


def dump_xml(serial: str, path: str) -> str:
    sh(serial, "uiautomator", "dump", path)
    return sh(serial, "cat", path)


def tap_bounds(serial: str, bounds: str) -> None:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
    if not m:
        return
    x1, y1, x2, y2 = map(int, m.groups())
    sh(serial, "input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2))


def find_drawer_opener(root: ET.Element) -> str | None:
    for node in root.iter("node"):
        desc = (node.attrib.get("content-desc") or "").lower()
        if "open navigation drawer" in desc or desc == "open drawer":
            b = node.attrib.get("bounds") or ""
            if b:
                return b
    return None


def device_labels_from_xml(xml: str) -> list[str]:
    labels: list[str] = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return labels
    for node in root.iter("node"):
        t = (node.attrib.get("text") or "").strip()
        if re.search(r"\[\d{3,5}\]", t):
            labels.append(t)
    return labels


def probe(serial: str) -> dict:
    sh(serial, "input", "keyevent", "KEYCODE_HOME")
    time.sleep(0.5)
    subprocess.run(
        ["adb", "-s", serial, "shell", "am", "start", "-W", "-n", "org.voidfix.smsgateway/.ui.StartActivity"],
        timeout=30,
    )
    time.sleep(2.5)
    xml1 = dump_xml(serial, "/data/local/tmp/vf_rob1.xml")
    root = ET.fromstring(xml1)
    bounds = find_drawer_opener(root)
    if bounds:
        tap_bounds(serial, bounds)
    else:
        sh(serial, "input", "tap", "80", "180")
    time.sleep(1.5)
    xml2 = dump_xml(serial, "/data/local/tmp/vf_rob2.xml")
    labels = device_labels_from_xml(xml2)
    if not labels:
        # scroll drawer if present
        sh(serial, "input", "swipe", "200", "800", "200", "400", "300")
        time.sleep(0.8)
        xml2 = dump_xml(serial, "/data/local/tmp/vf_rob3.xml")
        labels = device_labels_from_xml(xml2)
    sh(serial, "input", "keyevent", "KEYCODE_HOME")
    ids = sorted(set(re.findall(r"\[(\d{3,5})\]", "\n".join(labels))))
    return {
        "drawer_opener_bounds": bounds,
        "labels": labels,
        "vf_ids": ids,
        "xml_hint": xml1[:200],
    }


def main() -> None:
    out = {}
    for slot, serial in SERIALS.items():
        if subprocess.run(["adb", "-s", serial, "get-state"], capture_output=True, text=True).stdout.strip() != "device":
            out[str(slot)] = {"error": "offline"}
            continue
        out[str(slot)] = probe(serial)
    p = "_tmp_vf_drawer_robust.json"
    open(p, "w", encoding="utf-8").write(json.dumps(out, indent=2))
    print(p, json.dumps(out))


if __name__ == "__main__":
    main()
