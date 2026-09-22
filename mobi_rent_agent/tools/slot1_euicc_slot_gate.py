"""Read-only farm Slot 1 eUICC slot-mapping gate.

Targets 1C101FDF6009EZ only. Never writes slot_map.json. Never starts
main.py. Never sends an activation code. Prints mapping integers and
booleans only — no EID, IMEI, MSISDN, or LPA strings.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.slot_isolation import SlotIsolationPolicy
from infrastructure.adb_companion import AdbCommandRunner
from infrastructure.adb_slot_status import load_slot_map

SLOT = 1
SERIAL = "1C101FDF6009EZ"
MAP_PATH = Path(__file__).resolve().parents[1] / "slot_map.json"

_LIST_RE = re.compile(
    r"action:\s*GET_EUICC_PROFILE_INFO_LIST,\s*result:\s*(-?\d+),\s*params:\s*\{slotId=(-?\d+)\}"
)
_EMBEDDED_RE = re.compile(
    r"updateEmbeddedSubscriptions:\s*cardId=(-?\d+),\s*result="
    r"\[GetEuiccProfileInfoListResult:\s*result=(?:UNKNOWN\()?(-?\d+)"
)


@dataclass(frozen=True)
class EuiccSlotObservation:
    lpa_bound: bool | None = None
    lpa_last_slot_id: int | None = None
    lpa_last_profile_list_result: int | None = None
    lpa_last_card_id: int | None = None
    embedded_list_empty: bool | None = None
    euicc_enabled: bool | None = None
    gsm_sim_state: str | None = None

    def to_public_dict(self) -> dict:
        return asdict(self)


def parse_lpa_profile_list(econtroller: str) -> tuple[int | None, int | None]:
    matches = _LIST_RE.findall(econtroller or "")
    if not matches:
        return None, None
    result, slot_id = matches[-1]
    return int(slot_id), int(result)


def parse_embedded_card(isub: str) -> int | None:
    matches = _EMBEDDED_RE.findall(isub or "")
    if not matches:
        return None
    return int(matches[-1][0])


def parse_lpa_bound(econtroller: str) -> bool | None:
    text = econtroller or ""
    start = text.find("===== EUICC CONNECTOR =====")
    block = text[start : start + 4000] if start >= 0 else text
    service_lines = [ln.strip() for ln in block.splitlines() if "mEuiccService=" in ln]
    if service_lines:
        latest = service_lines[-1]
        if latest.endswith("=null") or latest.endswith("mEuiccService=null"):
            return False
        return True
    if "curState=ConnectedState" in block:
        return True
    if "curState=DisconnectedState" in block:
        return False
    return None


def parse_embedded_empty(isub: str) -> bool:
    return bool(re.search(r"Embedded subscriptions:\s*\[\s*\]", isub or ""))


def parse_euicc_enabled(econtroller: str, isub: str) -> bool | None:
    if "EuiccManager is enabled: true" in (econtroller or "") or "Euicc enabled=true" in (isub or ""):
        return True
    if "EuiccManager is enabled: false" in (econtroller or "") or "Euicc enabled=false" in (isub or ""):
        return False
    return None


def evaluate_slot_gate(observation: EuiccSlotObservation) -> tuple[bool, str]:
    if observation.lpa_last_slot_id is None or observation.lpa_last_profile_list_result is None:
        return False, "no GET_EUICC_PROFILE_INFO_LIST observation"
    if observation.lpa_last_slot_id < 0:
        return False, "lpa slotId is negative; eUICC card not mapped"
    if observation.lpa_last_profile_list_result != 0:
        return False, (
            f"profile list result {observation.lpa_last_profile_list_result} is not success"
        )
    if observation.lpa_last_card_id is not None and observation.lpa_last_card_id < 0:
        return False, "cardId is negative; eUICC card not mapped"
    return True, "slot mapped"


def observation_from_dumps(econtroller: str, isub: str, gsm_sim_state: str = "") -> EuiccSlotObservation:
    slot_id, list_result = parse_lpa_profile_list(econtroller)
    return EuiccSlotObservation(
        lpa_bound=parse_lpa_bound(econtroller),
        lpa_last_slot_id=slot_id,
        lpa_last_profile_list_result=list_result,
        lpa_last_card_id=parse_embedded_card(isub),
        embedded_list_empty=parse_embedded_empty(isub),
        euicc_enabled=parse_euicc_enabled(econtroller, isub),
        gsm_sim_state=(gsm_sim_state or "").strip() or None,
    )


def collect_euicc_slot_observation(runner: AdbCommandRunner, serial: str) -> EuiccSlotObservation:
    econtroller = runner.run(serial, ["shell", "dumpsys", "econtroller"]).stdout
    isub = runner.run(serial, ["shell", "dumpsys", "isub"]).stdout
    gsm = runner.run(serial, ["shell", "getprop", "gsm.sim.state"]).stdout
    return observation_from_dumps(econtroller, isub, gsm)


def resolve_slot1_serial(slot_map: dict[int, str], *, farm_slot1_serial: str = SERIAL) -> tuple[str, str | None]:
    serial = slot_map.get(SLOT)
    if serial != farm_slot1_serial:
        return "", "slot 1 serial mismatch; refusing"
    return serial, None


def main() -> int:
    isolation = SlotIsolationPolicy({SLOT})
    isolation.reject_if_outside(SLOT)
    slot_map = load_slot_map(MAP_PATH)
    serial, serial_error = resolve_slot1_serial(slot_map)
    report = {
        "target": "farm_slot_1",
        "serial": SERIAL,
        "success": False,
        "gate_ready": False,
    }
    if serial_error:
        report["error"] = serial_error
        print(json.dumps(report, indent=2))
        return 2
    runner = AdbCommandRunner("adb", 30)
    observation = collect_euicc_slot_observation(runner, serial)
    ready, reason = evaluate_slot_gate(observation)
    report.update(observation.to_public_dict())
    report["gate_ready"] = ready
    report["success"] = ready
    report["error"] = None if ready else reason
    print(json.dumps(report, indent=2))
    return 0 if ready else 3


if __name__ == "__main__":
    sys.exit(main())
