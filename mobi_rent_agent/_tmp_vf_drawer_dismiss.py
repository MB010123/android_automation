"""Dismiss VoidFix default-SMS dialog with NO (no default-app change), then read drawer ID."""
from __future__ import annotations

import json
import re
import subprocess
import time
import xml.etree.ElementTree as ET

SERIALS = {11: "1C071FDF6004MS", 17: "25061FDF6006KF", 19: "1C101FDF6009EZ"}


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


def dump(serial: str) -> ET.Element:
    sh(serial, "uiautomator", "dump", "/data/local/tmp/vf_d.xml")
    xml = sh(serial, "cat", "/data/local/tmp/vf_d.xml")
    return ET.fromstring(xml)


def tap_text(root: ET.Element, serial: str, text: str) -> bool:
    for node in root.iter("node"):
        if (node.attrib.get("text") or "").strip() == text:
            b = node.attrib.get("bounds") or ""
            m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
            if m:
                x1, y1, x2, y2 = map(int, m.groups())
                sh(serial, "input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2))
                return True
    return False


def open_drawer(serial: str) -> list[str]:
    root = dump(serial)
    for node in root.iter("node"):
        desc = (node.attrib.get("content-desc") or "").lower()
        if "open navigation drawer" in desc:
            b = node.attrib.get("bounds") or ""
            m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
            if m:
                x1, y1, x2, y2 = map(int, m.groups())
                sh(serial, "input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2))
                break
    time.sleep(1.2)
    root2 = dump(serial)
    labels = []
    for node in root2.iter("node"):
        t = (node.attrib.get("text") or "").strip()
        if re.search(r"\[\d{3,5}\]", t):
            labels.append(t)
    return labels


def probe(serial: str) -> dict:
    sh(serial, "input", "keyevent", "KEYCODE_HOME")
    subprocess.run(
        ["adb", "-s", serial, "shell", "am", "start", "-W", "-n", "org.voidfix.smsgateway/.ui.StartActivity"],
        timeout=30,
    )
    time.sleep(2)
    root = dump(serial)
    if any((n.attrib.get("text") or "") == "Set as default SMS app" for n in root.iter("node")):
        tap_text(root, serial, "NO")
        time.sleep(1)
    labels = open_drawer(serial)
    sh(serial, "input", "keyevent", "KEYCODE_HOME")
    return {"labels": labels, "vf_ids": sorted(set(re.findall(r"\[(\d{3,5})\]", "\n".join(labels))))}


def main() -> None:
    out = {str(k): probe(v) for k, v in SERIALS.items()}
    open("_tmp_vf_drawer_dismiss.json", "w", encoding="utf-8").write(json.dumps(out, indent=2))
    print(json.dumps(out))


if __name__ == "__main__":
    main()
