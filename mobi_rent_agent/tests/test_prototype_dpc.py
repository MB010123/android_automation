from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.prototype import POLICY_COMPLIANCE_ACTION, PROVISIONING_MODE_ACTION
from infrastructure.prototype_dpc import EXPECTED_HANDLERS, handlers_declared_in_manifest

MANIFEST = (
    Path(__file__).resolve().parents[2]
    / "android_companion"
    / "app"
    / "src"
    / "main"
    / "AndroidManifest.xml"
)


def test_manifest_declares_required_provisioning_handlers():
    text = MANIFEST.read_text(encoding="utf-8")
    declared = handlers_declared_in_manifest(text)
    assert declared["ACTION_GET_PROVISIONING_MODE"] is True
    assert declared["ACTION_ADMIN_POLICY_COMPLIANCE"] is True
    assert declared["DEVICE_ADMIN_ENABLED"] is True
    assert PROVISIONING_MODE_ACTION in text
    assert POLICY_COMPLIANCE_ACTION in text
    assert "GetProvisioningModeActivity" in text
    assert "AdminPolicyComplianceActivity" in text
    assert EXPECTED_HANDLERS["ACTION_GET_PROVISIONING_MODE"] == PROVISIONING_MODE_ACTION
