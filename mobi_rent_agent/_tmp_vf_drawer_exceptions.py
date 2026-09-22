"""Read-only VoidFix drawer device label on exception slots."""
from __future__ import annotations

import json
import re
import subprocess
import time
import xml.etree.ElementTree as ET

SLOTS = {
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


def drawer_device_labels(serial: str) -> list[str]:
    subprocess.run(
        ["adb", "-s", serial, "shell", "am", "start", "-W", "-n", "org.voidfix.smsgateway/.ui.StartActivity"],
        timeout=30,
    )
    time.sleep(2)
    sh(serial, "input", "tap", "80", "180")
    time.sleep(1.2)
    sh(serial, "uiautomator", "dump", "/data/local/tmp/vf_ex_dr.xml")
    xml = sh(serial, "cat", "/data/local/tmp/vf_ex_dr.xml")
    labels: list[str] = []
    try:
        for node in ET.fromstring(xml).iter("node"):
            t = (node.attrib.get("text") or "").strip()
            if t and ("[" in t and "]" in t):
                labels.append(t)
    except ET.ParseError:
        pass
    sh(serial, "input", "keyevent", "KEYCODE_HOME")
    return labels


def main() -> None:
    rows = []
    for slot, serial in SLOTS.items():
        if subprocess.run(["adb", "-s", serial, "get-state"], capture_output=True, text=True).stdout.strip() != "device":
            rows.append({"slot": slot, "serial": serial, "error": "adb offline"})
            continue
        labels = drawer_device_labels(serial)
        ids = sorted(set(re.findall(r"\[(\d{4})\]", "\n".join(labels))))
        rows.append({"slot": slot, "serial": serial, "drawer_device_labels": labels, "parsed_vf_ids": ids})
    path = "_tmp_vf_drawer_exceptions.json"
    open(path, "w", encoding="utf-8").write(json.dumps(rows, indent=2))
    print(path, rows)


if __name__ == "__main__":
    main()
