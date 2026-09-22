"""Read-only Slot 1 Android eSIM authorization inspection.

Never sends an activation code. Never touches Slots 2-20. Never writes
slot_map.json. Prints capability bits only — no EID, IMEI, MSISDN, or LPA.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.esim_capabilities import derive_esim_capabilities
from domain.slot_isolation import SlotIsolationPolicy
from infrastructure.adb_companion import AdbCommandRunner, AdbForwardedJsonClient
from infrastructure.android_authorization_probe import AdbAuthorizationProbe
from infrastructure.adb_slot_status import load_slot_map

SLOT_1 = 1
DEFAULT_MAP = Path(__file__).resolve().parents[1] / "slot_map.json"


def main() -> int:
    isolation = SlotIsolationPolicy({SLOT_1})
    isolation.reject_if_outside(SLOT_1)
    slot_map = load_slot_map(DEFAULT_MAP)
    serial = slot_map.get(SLOT_1)
    if not serial:
        print("slot 1 is not present in slot_map.json")
        return 2
    runner = AdbCommandRunner("adb", 12)
    companion = AdbForwardedJsonClient(runner, "mobi_rent.provisioning", 12)
    snapshot = AdbAuthorizationProbe(runner, companion, real_esim_flag=False).read(serial)
    caps = derive_esim_capabilities(snapshot)
    print("slot_id=1")
    print(f"euicc_feature={snapshot.euicc_feature}")
    print(f"euicc_enabled={snapshot.euicc_enabled}")
    print(f"lpa_package={snapshot.lpa_package}")
    print(f"companion_privileged={snapshot.companion_privileged}")
    print(f"has_write_embedded={snapshot.has_write_embedded}")
    print(f"has_carrier_privileges={snapshot.has_carrier_privileges}")
    print(f"device_owner={snapshot.device_owner}")
    print(f"profile_owner={snapshot.profile_owner}")
    print(f"has_managed_subscriptions_permission={snapshot.has_managed_subscriptions_permission}")
    print(f"real_esim_flag={snapshot.real_esim_flag}")
    print(f"provider_id={caps.provider_id}")
    print(f"unattended={caps.unattended}")
    print(f"can_download={caps.can_download}")
    print(f"can_switch={caps.can_switch}")
    print(f"can_delete={caps.can_delete}")
    print(f"requires_user_consent={caps.requires_user_consent}")
    print(f"authorization_source={caps.authorization_source}")
    print(f"reason={caps.reason}")
    return 0 if caps.authorization_source != "none" else 3


if __name__ == "__main__":
    sys.exit(main())
