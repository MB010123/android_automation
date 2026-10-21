"""One-off interactive probe for the Samsung S20 One UI eSIM flow.

Dumps the current UI tree over ADB and prints clickable/labelled nodes so we
can record exact texts/resource-ids for the automated activation task.

Usage:
  python tools/s20_ui_probe.py [--serial RFCNB056WEV] [--tap X Y] [--text "Add eSIM"]
  --tap taps first, waits, then dumps. --text taps the center of the first
  node whose text or content-desc contains the given string.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

DEFAULT_SERIAL = "RFCNB056WEV"
REMOTE_XML = "/sdcard/mobirent_ui_probe.xml"


def adb(serial: str, *args: str) -> str:
    completed = subprocess.run(
        ["adb", "-s", serial, *args], capture_output=True, text=True, timeout=30
    )
    return (completed.stdout or "") + (completed.stderr or "")


def dump(serial: str) -> ET.ElementTree:
    adb(serial, "shell", "uiautomator", "dump", REMOTE_XML)
    raw = adb(serial, "exec-out", "cat", REMOTE_XML)
    start = raw.find("<?xml")
    return ET.ElementTree(ET.fromstring(raw[start:]))


def center(bounds: str) -> tuple[int, int]:
    nums = [int(v) for v in re.findall(r"-?\d+", bounds or "")]
    return (nums[0] + nums[2]) // 2, (nums[1] + nums[3]) // 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial", default=DEFAULT_SERIAL)
    parser.add_argument("--tap", nargs=2, type=int, default=None)
    parser.add_argument("--text", default=None)
    parser.add_argument("--keyevent", default=None)
    parser.add_argument("--wait", type=float, default=2.5)
    args = parser.parse_args()

    if args.keyevent:
        adb(args.serial, "shell", "input", "keyevent", args.keyevent)
        time.sleep(args.wait)
    if args.tap:
        adb(args.serial, "shell", "input", "tap", str(args.tap[0]), str(args.tap[1]))
        time.sleep(args.wait)
    if args.text:
        tree = dump(args.serial)
        needle = args.text.lower()
        target = None
        for node in tree.iter("node"):
            label = (node.get("text") or "") + " " + (node.get("content-desc") or "")
            if needle in label.lower():
                target = node
                break
        if target is None:
            print(f"NOT FOUND: {args.text!r}")
            return 1
        x, y = center(target.get("bounds") or "")
        print(f"TAP {args.text!r} at ({x},{y}) bounds={target.get('bounds')}")
        adb(args.serial, "shell", "input", "tap", str(x), str(y))
        time.sleep(args.wait)

    focus = adb(args.serial, "shell", "dumpsys", "window")
    match = re.search(r"mCurrentFocus=Window\{[^}]* ([\w./$]+)\}", focus)
    print("FOCUS:", match.group(1) if match else "?")
    tree = dump(args.serial)
    for node in tree.iter("node"):
        text = node.get("text") or ""
        desc = node.get("content-desc") or ""
        rid = node.get("resource-id") or ""
        clickable = node.get("clickable") == "true"
        if text or desc or clickable:
            cls = (node.get("class") or "").rsplit(".", 1)[-1]
            flags = "C" if clickable else " "
            print(f"[{flags}] {cls:<20} rid={rid:<60} text={text!r} desc={desc!r} bounds={node.get('bounds')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
