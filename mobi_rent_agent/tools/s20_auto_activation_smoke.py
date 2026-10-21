"""Live smoke test for the Samsung S20 zero-touch eSIM activation task.

Runs the real ``esim_auto_activation`` Farm task against Slot 21 with a
syntactically valid but unreachable activation code. Expected outcome:
``activation_failed`` with final state ``NOT_FOUND`` — proving the deep link,
raw-code shell quoting, logcat state tracking, and cleanup work on the real
device without adding any profile.

Usage: python tools/s20_auto_activation_smoke.py [--code LPA:1$...]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from application.esim_auto_activation_farm_task import run_esim_auto_activation
from application.farm_task_types import FarmTaskRequest
from infrastructure.config import load_config

DEFAULT_CODE = "LPA:1$smdp.example.invalid$MOBIRENT-SMOKE-TEST"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot", type=int, default=21)
    parser.add_argument("--code", default=DEFAULT_CODE)
    parser.add_argument("--timeout", type=float, default=45.0)
    args = parser.parse_args()

    config = load_config()
    slot_map = {int(k): v for k, v in json.loads(Path(ROOT, "slot_map.json").read_text()).items()}
    request = FarmTaskRequest(
        job_id="smoke-s20-auto-activation",
        task_type="esim_auto_activation",
        farm_slot_id=args.slot,
        payload={"activation_code": args.code, "rental_id": "smoke", "actor": "renter"},
    )
    result = run_esim_auto_activation(
        adb_path=config.adb_path,
        slot_map=slot_map,
        request=request,
        agent_config=config,
        timeout_seconds=args.timeout,
    )
    print(json.dumps({
        "ok": result.ok,
        "http_status": result.http_status,
        "error": result.error,
        "message": result.message,
        "details": result.details,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
