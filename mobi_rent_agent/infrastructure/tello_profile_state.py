"""Parse sandbox eUICC profile state from public dumpsys text.

Never returns ICCID, EID, MSISDN, or activation codes.
Selects the active embedded profile dynamically (any carrier).
EuiccProfileInfo.PROFILE_STATE_DISABLED=0, PROFILE_STATE_ENABLED=1.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

PROFILE_STATE_DISABLED = 0
PROFILE_STATE_ENABLED = 1


@dataclass(frozen=True)
class TelloProfileObservation:
    present: bool
    state: int | None
    sim_slot_index: int | None
    subscription_id: int | None
    source: str
    brand: str = "none"
    registry_home: bool = False

    def to_public_dict(self) -> dict:
        return {
            "present": self.present,
            "state": self.state,
            "sim_slot_index": self.sim_slot_index,
            "subscription_id": self.subscription_id,
            "source": self.source,
            "brand": self.brand,
            "registry_home": self.registry_home,
        }

    def is_enabled(self) -> bool:
        return self.present and self.state == PROFILE_STATE_ENABLED


def parse_tello_profile(isub: str = "", econtroller: str = "") -> TelloProfileObservation:
    return parse_sandbox_profile(isub, econtroller, name_filter="Tello")


def parse_sandbox_profile(
    isub: str = "",
    econtroller: str = "",
    registry: str = "",
    name_filter: str | None = None,
) -> TelloProfileObservation:
    records = _isub_embedded_records(isub)
    lpa = _lpa_profiles(econtroller)
    if name_filter:
        token = name_filter.casefold()
        records = [
            rec
            for rec in records
            if token in rec["display_name"].casefold() or token in rec["carrier_name"].casefold()
        ]
        lpa = [
            item
            for item in lpa
            if token in item["nickname"].casefold() or token in item["spn"].casefold()
        ]
    candidates = _candidates(records, lpa)
    if not candidates:
        return TelloProfileObservation(False, None, None, None, "none", "none", False)
    chosen = _select_active(candidates)
    home = _registry_home_for(registry, chosen.brand)
    return TelloProfileObservation(
        chosen.present,
        chosen.state,
        chosen.sim_slot_index,
        chosen.subscription_id,
        chosen.source,
        chosen.brand,
        home,
    )


def switch_after_download_reached_enabled(observation: TelloProfileObservation) -> tuple[bool, str | None]:
    label = observation.brand if observation.brand != "none" else "profile"
    if not observation.present:
        return False, f"{label} is not present on the eUICC"
    if observation.state != PROFILE_STATE_ENABLED:
        return False, f"{label} state={observation.state}; enabled state=1 required"
    if observation.sim_slot_index is not None and observation.sim_slot_index < 0:
        return False, f"{label} is enabled in LPA but not mapped to a SIM slot"
    return True, None


def collect_tello_profile(runner, serial: str) -> TelloProfileObservation:
    return collect_sandbox_profile(runner, serial, name_filter="Tello")


def collect_sandbox_profile(
    runner,
    serial: str,
    name_filter: str | None = None,
) -> TelloProfileObservation:
    isub = _shell(runner, serial, ["dumpsys", "isub"])
    econtroller = _shell(runner, serial, ["dumpsys", "econtroller"])
    registry = _shell(runner, serial, ["dumpsys", "telephony.registry"])
    return parse_sandbox_profile(isub, econtroller, registry, name_filter=name_filter)


def _candidates(records: list[dict], lpa: list[dict]) -> list[TelloProfileObservation]:
    unused_lpa = list(lpa)
    out: list[TelloProfileObservation] = []
    for rec in records:
        matched = _take_lpa(unused_lpa, rec)
        if matched is not None:
            state = matched["state"]
            source = "lpa"
            brand = rec["display_name"] or rec["carrier_name"] or matched["nickname"] or matched["spn"]
        else:
            state = (
                PROFILE_STATE_ENABLED
                if rec["sim_slot_index"] >= 0
                else PROFILE_STATE_DISABLED
            )
            source = "isub"
            brand = rec["display_name"] or rec["carrier_name"] or "embedded"
        out.append(
            TelloProfileObservation(
                True,
                state,
                rec["sim_slot_index"],
                rec["subscription_id"],
                source,
                brand or "embedded",
                False,
            )
        )
    if out:
        return out
    for item in lpa:
        out.append(
            TelloProfileObservation(
                True,
                item["state"],
                None,
                None,
                "lpa",
                item["nickname"] or item["spn"] or "embedded",
                False,
            )
        )
    return out


def _select_active(candidates: list[TelloProfileObservation]) -> TelloProfileObservation:
    enabled = [
        item
        for item in candidates
        if item.state == PROFILE_STATE_ENABLED
        and (item.sim_slot_index is None or item.sim_slot_index >= 0)
    ]
    mapped = [item for item in enabled if item.sim_slot_index is not None and item.sim_slot_index >= 0]
    if mapped:
        return mapped[0]
    if enabled:
        return enabled[0]
    return candidates[0]


def _take_lpa(unused: list[dict], rec: dict) -> dict | None:
    names = {rec["display_name"].casefold(), rec["carrier_name"].casefold()} - {""}
    for index, item in enumerate(unused):
        blob = {item["nickname"].casefold(), item["spn"].casefold()} - {""}
        if names & blob:
            return unused.pop(index)
    return None


def _lpa_profiles(econtroller: str) -> list[dict]:
    found: list[dict] = []
    for match in re.finditer(r"EuiccProfileInfo \(([^)]*)\)", econtroller or ""):
        body = match.group(1)
        state = re.search(r"state=(\d+)", body)
        if not state:
            continue
        nick = re.search(r"nickname=([^,]*)", body)
        spn = re.search(r"serviceProviderName=([^,]*)", body)
        found.append(
            {
                "nickname": (nick.group(1).strip() if nick else ""),
                "spn": (spn.group(1).strip() if spn else ""),
                "state": int(state.group(1)),
            }
        )
    for match in re.finditer(
        r"Profile nickname = ([^\n.]+)\.[\s\S]{0,400}?state=(\d+)",
        econtroller or "",
        re.I,
    ):
        found.append(
            {
                "nickname": match.group(1).strip(),
                "spn": "",
                "state": int(match.group(2)),
            }
        )
    return found


def _registry_home_for(registry: str, brand: str) -> bool:
    text = registry or ""
    if not text or not re.search(r"registrationState=HOME", text):
        return False
    if brand and brand != "none" and re.search(re.escape(brand), text, re.I):
        return True
    return bool(re.search(r"mIsEmergencyOnly=false", text)) and bool(
        re.search(r"registrationState=HOME", text)
    )


def _isub_embedded_records(isub: str) -> list[dict]:
    records: list[dict] = []
    seen: set[tuple[int, int, str]] = set()
    for block in re.split(r"\[SubscriptionInfoInternal:", isub or ""):
        if re.search(r"isEmbedded=0\b", block) and not re.search(r"isEmbedded=1\b", block):
            continue
        if not re.search(r"isEmbedded=1\b", block):
            continue
        id_match = re.search(r"\bid=(\d+)\b", block)
        slot_match = re.search(r"simSlotIndex=(-?\d+)", block)
        if not id_match or not slot_match:
            continue
        display = _field(block, "displayName")
        carrier = _field(block, "carrierName")
        key = (int(id_match.group(1)), int(slot_match.group(1)), display)
        if key in seen:
            continue
        seen.add(key)
        records.append(
            {
                "subscription_id": int(id_match.group(1)),
                "sim_slot_index": int(slot_match.group(1)),
                "display_name": display,
                "carrier_name": carrier,
            }
        )
    return records


def _field(block: str, key: str) -> str:
    match = re.search(rf"\b{key}=(.*?)(?=\s+\w+=|$)", block)
    return match.group(1).strip() if match else ""


def _shell(runner, serial: str, arguments: list[str]) -> str:
    try:
        return runner.run(serial, ["shell", *arguments]).stdout
    except Exception:
        return ""
