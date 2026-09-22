"""Quarantined Slot 1 silent-provision tool.

This tool is not started by the daemon. It never sends provision_esim,
never reads or writes an activation code, and never calls EuiccManager.
It exists only to refuse leftover silent-install attempts behind the
same SlotIsolationPolicy / REAL_ESIM / can_silent_install contract.

Usage (from mobi_rent_agent/):

    python tools/slot1_tello_provision.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infrastructure.legacy_silent_guard import evaluate_legacy_tool_request

SLOT = 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Quarantined Slot 1 silent-provision tool")
    parser.add_argument("--slot", type=int, default=SLOT)
    args = parser.parse_args()
    reason = evaluate_legacy_tool_request(args.slot)
    report = {
        "slot": args.slot,
        "blocker": reason,
        "activation_code_sent": False,
        "success": False,
    }
    print(json.dumps(report, indent=2))
    print("LEGACY SILENT PROVISION REFUSED", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
