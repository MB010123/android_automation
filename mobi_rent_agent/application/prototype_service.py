"""Isolated prototype orchestration over existing ports and providers.

Does not start the farm daemon, does not write slot_map.json, and does not
use the bay-scoped farm SMS dispatcher. SMS goes through the prototype-only
JSON VoidFix client only after every real-send gate passes. VoidFix slot is
derived from a live Android simSlotIndex check (US Mobile = 1 → slot=1).
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from domain.esim_capabilities import AndroidAuthorizationSnapshot, derive_esim_capabilities
from domain.models import ActivationJob, SmsSendOutcome
from domain.ports import SmsGateway
from domain.prototype import (
    DPC_PACKAGE,
    PROTOTYPE_DEVICE_ID,
    PROTOTYPE_VOIDFIX_DEVICE_ID,
    PrototypeMode,
    PrototypeOperationType,
    redact_msisdn,
)
from domain.provisioning_state import ActivationVerdict, JobState, apply_transition
from domain.verification import FourLayerVerification, evaluate_activation
from infrastructure.human_activation_provider import HumanActivationProvider
from infrastructure.prototype_audit import PrototypeAuditStore
from infrastructure.prototype_config import PrototypeConfig, PrototypeDevice
from infrastructure.prototype_dpc import build_dpc_report, owner_package_from_dump
from infrastructure.prototype_voidfix_api import (
    HARMLESS_TEST_SMS,
    JSON_CONTENT_TYPE,
    REQUIRED_ANDROID_SIM_SLOT_INDEX,
    REQUIRED_API_SLOT,
    SEND_PHP_HAS_CONFIRMED_SLOT_PARAMETER,
    voidfix_slot_from_live_android,
)
from infrastructure.voidfix_api import SmsGatewayError

INBOUND_STATUS = "unsupported_pending_provider_documentation"


class PrototypeRefuse(RuntimeError):
    """Unsafe or incomplete prototype request."""


class PrototypeService:
    def __init__(
        self,
        config: PrototypeConfig,
        *,
        gateway: SmsGateway | None = None,
        audit: PrototypeAuditStore | None = None,
        monotonic: Any = time.monotonic,
    ) -> None:
        self._config = config
        self._gateway = gateway
        self._audit = audit
        self._monotonic = monotonic

    def audit(self) -> dict[str, Any]:
        return {
            "environment": self._config.environment,
            "mode": self._config.mode.value,
            "production_disabled": self._config.mode is PrototypeMode.PRODUCTION_DISABLED
            or not self._config.voidfix_enabled,
            "voidfix_enabled": self._config.voidfix_enabled,
            "real_send_confirmation": self._config.real_send_confirmation,
            "api_key_present": bool(self._config.voidfix_api_key),
            "allowlist": list(self._config.allowlist),
            "device_count": len(self._config.devices),
            "inbound_sms": INBOUND_STATUS,
            "dpc_package": DPC_PACKAGE,
            "mutations": False,
            "sms_sent": False,
        }

    def voidfix_status(self) -> dict[str, Any]:
        devices = []
        for device in self._config.devices.values():
            devices.append(
                {
                    "device_id": device.device_id,
                    "voidfix_mapped": bool(device.voidfix_device_id),
                    "enabled": device.enabled,
                    "in_allowlist": device.device_id in self._config.allowlist,
                }
            )
        return {
            "environment": self._config.environment,
            "mode": self._config.mode.value,
            "voidfix_enabled": self._config.voidfix_enabled,
            "send_endpoint": self._config.voidfix_send_endpoint,
            "send_endpoint_https": self._config.voidfix_send_endpoint.lower().startswith("https://"),
            "api_key_present": bool(self._config.voidfix_api_key),
            "real_send_confirmation": self._config.real_send_confirmation,
            "recipient_allowlist_count": len(self._config.recipient_allowlist),
            "inbound_sms": INBOUND_STATUS,
            "inbound_endpoint_configured": bool(self._config.voidfix_inbound_endpoint),
            "content_type": JSON_CONTENT_TYPE,
            "confirmed_slot_parameter": SEND_PHP_HAS_CONFIRMED_SLOT_PARAMETER,
            "required_api_slot": REQUIRED_API_SLOT,
            "required_android_sim_slot_index": REQUIRED_ANDROID_SIM_SLOT_INDEX,
            "slot_requires_live_android_index": True,
            "devices": devices,
            "sms_sent": False,
            "automatic_retries": False,
        }

    def device_status(
        self,
        device_id: str,
        snapshot: AndroidAuthorizationSnapshot | None = None,
        device_policy_dump: str = "",
        handlers: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        device = self._require_device(device_id, require_enabled=False)
        snap = snapshot or AndroidAuthorizationSnapshot()
        owner_package = owner_package_from_dump(device_policy_dump) if device_policy_dump else None
        report = build_dpc_report(
            snap,
            device_owner_package=owner_package,
            handlers=handlers,
        )
        report["test_device_id"] = device.device_id
        report["device_type"] = device.device_type
        report["adb_serial_configured"] = bool(device.adb_serial)
        return report

    def esim_status(
        self,
        device_id: str,
        snapshot: AndroidAuthorizationSnapshot | None = None,
        verification: FourLayerVerification | None = None,
    ) -> dict[str, Any]:
        device = self._require_device(device_id, require_enabled=False)
        snap = snapshot or AndroidAuthorizationSnapshot()
        caps = derive_esim_capabilities(snap)
        verdict = evaluate_activation(verification) if verification else ActivationVerdict.VERIFICATION_UNKNOWN
        return {
            "test_device_id": device.device_id,
            "activation_method": "human_tello_settings_lpa",
            "user_interaction_required": caps.requires_user_consent,
            "unattended": caps.unattended,
            "can_silent_install": caps.can_download,
            "can_silent_switch": caps.can_switch,
            "automatic_retries": False,
            "authorization_source": caps.authorization_source,
            "verification_verdict": verdict.value,
            "success": verdict.success,
            "reason": caps.reason,
        }

    def dry_run(
        self,
        device_id: str,
        verification: FourLayerVerification | None = None,
    ) -> dict[str, Any]:
        self._require_mode({PrototypeMode.DRY_RUN, PrototypeMode.REAL_TEST}, "dry-run")
        device = self._require_device(device_id)
        provider = HumanActivationProvider()
        job = ActivationJob(job_id=f"prototype-esim-{device.device_id}", slot_id=1, qr_url="https://example.invalid/lpa")
        history = [JobState.PENDING.value]
        state = apply_transition(JobState.PENDING, JobState.CLAIMED)
        history.append(state.value)
        state = apply_transition(state, JobState.PREFLIGHT)
        history.append(state.value)
        submitted = provider.submit_activation(device.adb_serial or "unspecified", job)
        state = submitted.state
        history.append(state.value)
        if verification is not None:
            result = provider.verify_profile(device.adb_serial or "unspecified", job, verification)
            verdict = result.verdict or ActivationVerdict.VERIFICATION_UNKNOWN
        else:
            result = None
            verdict = ActivationVerdict.VERIFICATION_UNKNOWN
        return {
            "environment": self._config.environment,
            "mode": self._config.mode.value,
            "test_device_id": device.device_id,
            "activation_method": "human_tello_settings_lpa",
            "user_interaction_required": True,
            "activation_code_sent": submitted.activation_code_sent,
            "automatic_retries": False,
            "job_states": history,
            "job_state": state.value,
            "verification_verdict": verdict.value,
            "success": verdict.success,
            "sms_simulated": True,
            "sms_sent": False,
            "mutations": False,
            "error": submitted.error if result is None else result.error,
        }

    def send_test_sms(
        self,
        device_id: str,
        recipient: str,
        *,
        confirm: bool,
        operation_id: str | None = None,
        message: str = HARMLESS_TEST_SMS,
        android_sim_slot_index: int | None = None,
    ) -> dict[str, Any]:
        device = self._require_device(device_id)
        op_id = (operation_id or "").strip() or f"proto-sms-{uuid.uuid4()}"
        if self._audit:
            existing = self._audit.get(op_id)
            if existing:
                existing["duplicate_prevented"] = True
                existing["sms_sent"] = False
                return existing
        refusal = self._real_send_refusal(
            device,
            recipient,
            confirm,
            android_sim_slot_index=android_sim_slot_index,
        )
        if refusal:
            self._store_sms_record(
                op_id,
                device,
                recipient,
                status="refused",
                error=refusal,
                sms_sent=False,
            )
            raise PrototypeRefuse(refusal)

        if self._gateway is None:
            raise PrototypeRefuse("SMS gateway is not configured")

        started = self._monotonic()
        device_ids = [device.voidfix_device_id or ""]
        sender = getattr(self._gateway, "send_with_live_android_slot", None)
        if sender is not None:
            result = sender(recipient, message, device_ids, android_sim_slot_index)
        else:
            result = self._gateway.send(recipient, message, device_ids)
        latency_ms = int((self._monotonic() - started) * 1000)
        status = (result.outcome or SmsSendOutcome.FAILURE).value
        return self._store_sms_record(
            op_id,
            device,
            recipient,
            status=status,
            error=result.error,
            sms_sent=bool(result.success),
            outcome=status,
            provider_message_id=result.provider_message_id,
            latency_ms=latency_ms,
        )

    def preview_send(self, device_id: str, recipient: str) -> dict[str, str]:
        device = self._require_device(device_id, require_enabled=False)
        return {
            "Environment": self._config.environment,
            "Device": device.device_id,
            "VoidFix device": "configured" if device.voidfix_device_id else "missing",
            "Recipient": redact_msisdn(recipient),
            "Content-Type": JSON_CONTENT_TYPE,
            "SIM selector": "slot=1 after live Android simSlotIndex=1",
            "Real send confirmation": "enabled" if self._config.real_send_confirmation else "disabled",
        }

    def report(
        self,
        device_id: str,
        snapshot: AndroidAuthorizationSnapshot | None = None,
        verification: FourLayerVerification | None = None,
        device_policy_dump: str = "",
    ) -> dict[str, Any]:
        return {
            "audit": self.audit(),
            "device_status": self.device_status(
                device_id, snapshot=snapshot, device_policy_dump=device_policy_dump
            ),
            "esim_status": self.esim_status(device_id, snapshot=snapshot, verification=verification),
            "voidfix_status": self.voidfix_status(),
        }

    def _real_send_refusal(
        self,
        device: PrototypeDevice,
        recipient: str,
        confirm: bool,
        *,
        android_sim_slot_index: int | None = None,
    ) -> str | None:
        if self._config.mode is PrototypeMode.PRODUCTION_DISABLED:
            return "production features are disabled"
        if self._config.mode is PrototypeMode.AUDIT:
            return "AUDIT mode cannot send SMS"
        if self._config.mode is PrototypeMode.DRY_RUN:
            return "DRY_RUN mode cannot send a real SMS"
        if self._config.mode is not PrototypeMode.REAL_TEST:
            return "REAL_TEST mode is required for a real SMS"
        if not self._config.voidfix_enabled:
            return "VOIDFIX_ENABLED is false"
        if not self._config.voidfix_api_key:
            return "VOIDFIX_API_KEY is missing"
        if not self._config.real_send_confirmation:
            return "VOIDFIX_REAL_SEND_CONFIRMATION is false"
        if not confirm:
            return "CLI --confirm is required"
        if device.device_id not in self._config.allowlist:
            return f"{device.device_id} is not on the prototype allowlist"
        if not device.enabled:
            return f"{device.device_id} is not enabled"
        if not device.voidfix_device_id:
            return f"{device.device_id} has no VoidFix device mapping"
        if not self._config.recipient_allowlist:
            return "prototype recipient allowlist is empty"
        if recipient not in self._config.recipient_allowlist:
            return "recipient is not on the prototype allowlist"
        if not self._config.voidfix_send_endpoint.lower().startswith("https://"):
            return "VoidFix send endpoint must use HTTPS"
        if _is_farm_slot_id(device.device_id):
            return f"{device.device_id} is a production farm slot ID"
        if device.device_id != PROTOTYPE_DEVICE_ID:
            return f"{device.device_id} is not the isolated prototype device {PROTOTYPE_DEVICE_ID}"
        if str(device.voidfix_device_id) != str(PROTOTYPE_VOIDFIX_DEVICE_ID):
            return f"{device.device_id} must map to VoidFix device {PROTOTYPE_VOIDFIX_DEVICE_ID}"
        try:
            voidfix_slot_from_live_android(android_sim_slot_index)
        except SmsGatewayError as exc:
            return str(exc)
        return None

    def _require_mode(self, allowed: set[PrototypeMode], action: str) -> None:
        if self._config.mode not in allowed:
            raise PrototypeRefuse(f"{action} is not allowed in mode {self._config.mode.value}")

    def _require_device(self, device_id: str, *, require_enabled: bool = True) -> PrototypeDevice:
        if _is_farm_slot_id(device_id):
            raise PrototypeRefuse(f"{device_id} is a production farm slot ID")
        if device_id not in self._config.devices:
            raise PrototypeRefuse(f"unknown prototype device {device_id}")
        device = self._config.devices[device_id]
        if device.adb_serial and device.adb_serial in self._config.production_serials:
            raise PrototypeRefuse(f"{device_id} is a production farm serial")
        if require_enabled and device.device_id not in self._config.allowlist:
            raise PrototypeRefuse(f"{device_id} is not on the prototype allowlist")
        return device

    def _store_sms_record(
        self,
        operation_id: str,
        device: PrototypeDevice,
        recipient: str,
        *,
        status: str,
        error: str | None,
        sms_sent: bool,
        outcome: str | None = None,
        provider_message_id: str | None = None,
        latency_ms: int | None = None,
    ) -> dict[str, Any]:
        record = {
            "operation_id": operation_id,
            "environment": self._config.environment,
            "test_device_id": device.device_id,
            "voidfix_device_id": device.voidfix_device_id,
            "operation_type": PrototypeOperationType.SEND_TEST_SMS.value,
            "recipient": recipient,
            "status": status,
            "outcome": outcome,
            "verification_status": None,
            "error_code": None if sms_sent or status in {"dry_run_simulated"} else "refused_or_failed",
            "safe_error_message": error,
            "provider_message_id": provider_message_id,
            "latency_ms": latency_ms,
            "sms_sent": sms_sent,
            "automatic_retries": False,
        }
        if self._audit:
            return self._audit.append(record)
        record["recipient"] = redact_msisdn(recipient)
        return record


def _is_farm_slot_id(device_id: str) -> bool:
    return device_id.isdigit() and 1 <= int(device_id) <= 20
