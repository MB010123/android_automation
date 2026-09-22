"""Production wiring for VoidFix `SmsDispatchService` (composition root helper)."""
from __future__ import annotations

import logging
from pathlib import Path

from application.sms_outbox import SmsOutbox
from infrastructure.sms_outbox_store import SqliteSmsOutboxStore
from application.slot_coordinator import SlotOperationCoordinator
from application.sms_service import SmsDispatchService
from domain.farm import PROTOTYPE_VOIDFIX_DEVICE_ID, SmsRetryPolicy, VoidFixDeliveryPollPolicy
from domain.slot_isolation import SlotIsolationError, SlotIsolationPolicy
from infrastructure.config import AgentConfig
from infrastructure.voidfix_api import SmsGatewayError, VoidFixSmsGateway
from infrastructure.voidfix_delivery import VoidFixDeliveryPoller
from infrastructure.voidfix_devices import VoidFixDeviceMapError, load_voidfix_device_map, load_voidfix_sim_slots

logger = logging.getLogger("mobi_rent_agent.sms_factory")

# VoidFix send.php `simSlot=1` selects dashboard SIM #2 (US Mobile on farm Pixel 6).
DEFAULT_VOIDFIX_SEND_SIM_SLOT = 1
DEFAULT_SMS_OUTBOX_DB = Path(__file__).resolve().parents[1] / "logs" / "sms_outbox.sqlite"


def create_durable_sms_outbox(path: Path | None = None) -> SmsOutbox:
    db_path = path or DEFAULT_SMS_OUTBOX_DB
    return SmsOutbox(store=SqliteSmsOutboxStore(db_path))


def build_sms_dispatch_service(
    config: AgentConfig,
    coordinator: SlotOperationCoordinator | None = None,
    outbox: SmsOutbox | None = None,
) -> SmsDispatchService | None:
    """Construct production SMS dispatch with delivery polling when configured."""
    if not config.sms_enabled:
        return None
    if not config.voidfix_api_key:
        logger.error("voidfix-sms enabled but VOIDFIX_API_KEY is missing")
        return None

    isolation = SlotIsolationPolicy(config.voidfix_allowed_slot_ids)
    try:
        isolation.refuse_if_empty({1: "placeholder"})
    except SlotIsolationError as exc:
        logger.error("voidfix-sms refused by slot isolation: %s", exc)
        return None

    try:
        device_map = _load_production_device_map(config)
    except VoidFixDeviceMapError as exc:
        logger.error("voidfix-sms device map invalid: %s", exc)
        return None

    if not device_map:
        logger.error("voidfix-sms device map is empty after allowlist filtering")
        return None

    try:
        gateway = VoidFixSmsGateway(
            api_key=config.voidfix_api_key,
            send_endpoint=config.voidfix_send_endpoint,
            inbound_endpoint=config.voidfix_inbound_endpoint,
            timeout_seconds=config.request_timeout_seconds,
        )
    except SmsGatewayError as exc:
        logger.error("voidfix-sms gateway misconfigured: %s", exc)
        return None

    delivery_poller = _build_delivery_poller(config)
    voidfix_sim_slots = _resolve_voidfix_sim_slots(config, device_map)
    slot_ids = sorted(set(device_map) | set(isolation.allowed_slot_ids))
    coord = coordinator or SlotOperationCoordinator(slot_ids)
    outbox_path = Path(config.sms_outbox_db_path) if config.sms_outbox_db_path else None
    resolved_outbox = outbox if outbox is not None else create_durable_sms_outbox(outbox_path)

    return SmsDispatchService(
        gateway=gateway,
        isolation=isolation,
        device_map=device_map,
        coordinator=coord,
        webhook_secret=config.voidfix_webhook_secret,
        recipient_allowlist=config.voidfix_recipient_allowlist,
        live_send_authorized=config.voidfix_live_send_authorized,
        dry_run=config.voidfix_dry_run,
        outbox=resolved_outbox,
        retry_policy=SmsRetryPolicy(max_attempts=config.sms_max_attempts),
        delivery_poller=delivery_poller,
        delivery_policy=VoidFixDeliveryPollPolicy(
            interval_seconds=config.voidfix_delivery_poll_interval_seconds,
            timeout_seconds=config.voidfix_delivery_poll_timeout_seconds,
        ),
        voidfix_sim_slots=voidfix_sim_slots,
    )


def recover_sms_outbox_after_restart(config: AgentConfig) -> SmsDispatchService | None:
    """Load durable outbox from disk. Caller may resume delivery polling in a background thread."""
    service = build_sms_dispatch_service(config)
    if service is None:
        return None
    non_terminal = service._outbox.list_non_terminal()
    if non_terminal:
        logger.info(
            "voidfix-sms reloaded %s non-terminal outbox record(s) from durable store",
            len(non_terminal),
        )
    return service


def sms_dispatch_runtime_flags(config: AgentConfig) -> dict[str, object]:
    """Non-secret snapshot for startup logs and readiness checks."""
    service = build_sms_dispatch_service(config)
    return {
        "sms_enabled": config.sms_enabled,
        "delivery_poll_enabled": config.voidfix_delivery_poll_enabled,
        "sim_slot_send_enabled": config.voidfix_sim_slot_send_enabled,
        "dispatch_service_constructed": service is not None,
        "delivery_poller_active": service is not None and service._delivery_poller is not None,
        "voidfix_allowed_slot_ids": list(config.voidfix_allowed_slot_ids),
    }


def _load_production_device_map(config: AgentConfig) -> dict[int, str]:
    path = config.voidfix_device_map_path
    if not path:
        raise VoidFixDeviceMapError("VOIDFIX_DEVICE_MAP_PATH is not set")
    raw = load_voidfix_device_map(path)
    allowed = set(config.voidfix_allowed_slot_ids)
    filtered: dict[int, str] = {}
    for slot_id, device_id in raw.items():
        if str(device_id).strip() == PROTOTYPE_VOIDFIX_DEVICE_ID:
            logger.warning(
                "voidfix-sms ignoring slot %s mapped to prototype device %s",
                slot_id,
                PROTOTYPE_VOIDFIX_DEVICE_ID,
            )
            continue
        if allowed and slot_id not in allowed:
            continue
        filtered[slot_id] = str(device_id).strip()
    if PROTOTYPE_VOIDFIX_DEVICE_ID in set(raw.values()) and not any(
        str(v) == PROTOTYPE_VOIDFIX_DEVICE_ID for v in filtered.values()
    ):
        pass  # prototype entry excluded from production map
    missing = [slot for slot in allowed if slot not in filtered]
    if missing:
        raise VoidFixDeviceMapError(
            f"allowed VoidFix slots missing from device map: {missing}"
        )
    return filtered


def _build_delivery_poller(config: AgentConfig) -> VoidFixDeliveryPoller | None:
    if not config.voidfix_delivery_poll_enabled:
        return None
    return VoidFixDeliveryPoller(
        config.voidfix_api_key or "",
        read_messages_endpoint=config.voidfix_read_messages_endpoint,
        timeout_seconds=config.request_timeout_seconds,
    )


def _resolve_voidfix_sim_slots(
    config: AgentConfig,
    device_map: dict[int, str],
) -> dict[int, int]:
    if not config.voidfix_sim_slot_send_enabled:
        return {}
    path = config.voidfix_device_map_path
    from_file = load_voidfix_sim_slots(Path(path)) if path else {}
    resolved: dict[int, int] = {}
    for slot_id in device_map:
        resolved[slot_id] = from_file.get(slot_id, DEFAULT_VOIDFIX_SEND_SIM_SLOT)
    return resolved
