"""Read-only production configuration validation (no SMS, no ADB side effects)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from infrastructure.adb_slot_status import SlotMapError, load_slot_map
from infrastructure.config import AgentConfig, ConfigError, load_config
from infrastructure.voidfix_devices import VoidFixDeviceMapError, load_voidfix_device_map


@dataclass
class ValidationIssue:
    level: str  # "error" | "warn"
    code: str
    message: str


@dataclass
class ProductionValidationReport:
    ok: bool
    app_name: str
    issues: list[ValidationIssue] = field(default_factory=list)

    def add(self, level: str, code: str, message: str) -> None:
        self.issues.append(ValidationIssue(level=level, code=code, message=message))


def validate_production_config(
    *,
    env_file: str | None = ".env",
    project_root: Path | None = None,
    expect_farm_slots: int = 20,
    role: str | None = None,
) -> ProductionValidationReport:
    """Validate agent config and on-disk farm maps without network or SMS."""
    import os

    root = project_root or Path.cwd()
    report = ProductionValidationReport(ok=True, app_name="mobi-rent-agent")
    resolved_role = (role or os.getenv("MOBI_RENT_DEPLOY_ROLE") or "farm").strip().lower()

    try:
        config = load_config(env_file=env_file)
    except ConfigError as exc:
        report.ok = False
        report.add("error", "config_load", str(exc))
        return report

    report.app_name = config.app_name

    _validate_paths(config, root, report)

    if resolved_role == "vps":
        _validate_vps_role(config, root, report, expect_farm_slots)
    else:
        _validate_slot_map(config, root, report, expect_farm_slots)
        _validate_voidfix_map(config, root, report, expect_farm_slots)
        _validate_sms_flags(config, report)
        _validate_farm_agent_token(report)

    if any(i.level == "error" for i in report.issues):
        report.ok = False
    return report


def _validate_vps_role(
    config: AgentConfig,
    root: Path,
    report: ProductionValidationReport,
    expect_farm_slots: int,
) -> None:
    import os

    if not config.voidfix_device_map_path:
        report.add("error", "voidfix_map_path", "VOIDFIX_DEVICE_MAP_PATH required on VPS for inbound slot mapping")
    else:
        map_path = _resolve_path(config.voidfix_device_map_path, root)
        if map_path is None or not map_path.exists():
            report.add("error", "voidfix_map_missing", f"VoidFix device map not found: {map_path}")
        else:
            try:
                vf_map = load_voidfix_device_map(map_path)
                if len(vf_map) != expect_farm_slots:
                    report.add(
                        "warn",
                        "voidfix_map_count",
                        f"VPS VoidFix map has {len(vf_map)} slots; expected {expect_farm_slots}",
                    )
            except VoidFixDeviceMapError as exc:
                report.add("error", "voidfix_map_invalid", str(exc))

    if not os.getenv("FARM_AGENT_URL"):
        report.add("warn", "farm_agent_url", "FARM_AGENT_URL unset; /farm/status proxy will be unavailable")
    if not os.getenv("FARM_AGENT_API_TOKEN"):
        report.add("error", "farm_agent_token", "FARM_AGENT_API_TOKEN required on VPS for farm status proxy")

    inbound_db = os.getenv("INBOUND_MESSAGES_DB_PATH")
    if inbound_db:
        path = _resolve_path(inbound_db, root)
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                report.add("error", "inbound_db_dir", str(exc))


def _validate_farm_agent_token(report: ProductionValidationReport) -> None:
    import os

    if not os.getenv("FARM_AGENT_API_TOKEN"):
        report.add(
            "warn",
            "farm_agent_token",
            "FARM_AGENT_API_TOKEN unset; farm status server cannot start securely",
        )


def _resolve_path(raw: str | None, root: Path) -> Path | None:
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    return path


def _validate_paths(config: AgentConfig, root: Path, report: ProductionValidationReport) -> None:
    log_path = Path(config.log_file)
    if not log_path.is_absolute():
        log_path = root / log_path
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        report.add("error", "log_dir", f"Cannot create log directory {log_path.parent}: {exc}")

    if config.sms_outbox_db_path:
        outbox = _resolve_path(config.sms_outbox_db_path, root)
        if outbox is not None:
            try:
                outbox.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                report.add("error", "outbox_dir", f"Cannot create outbox directory {outbox.parent}: {exc}")


def _validate_slot_map(
    config: AgentConfig,
    root: Path,
    report: ProductionValidationReport,
    expect_farm_slots: int,
) -> None:
    default_map = root / "slot_map.json"
    map_path = _resolve_path(config.slot_map_path, root) if config.slot_map_path else default_map
    if map_path is None or not map_path.exists():
        report.add(
            "warn",
            "slot_map_missing",
            f"slot map not found at {map_path} (required on phone-farm host with ADB)",
        )
        return
    try:
        slot_map = load_slot_map(map_path)
    except SlotMapError as exc:
        report.add("error", "slot_map_invalid", str(exc))
        return
    if len(slot_map) != expect_farm_slots:
        report.add(
            "warn",
            "slot_map_count",
            f"slot map has {len(slot_map)} entries; expected {expect_farm_slots} for full farm",
        )
    missing = [s for s in range(1, expect_farm_slots + 1) if s not in slot_map]
    if missing:
        report.add("error", "slot_map_incomplete", f"missing slot ids: {missing[:10]}{'...' if len(missing) > 10 else ''}")


def _validate_voidfix_map(
    config: AgentConfig,
    root: Path,
    report: ProductionValidationReport,
    expect_farm_slots: int,
) -> None:
    if not config.sms_enabled:
        report.add("warn", "voidfix_sms_disabled", "VOIDFIX SMS dispatch disabled (VOIDFIX_ENABLED=false or no key)")
        return
    if not config.voidfix_device_map_path:
        report.add("error", "voidfix_map_path", "VOIDFIX_DEVICE_MAP_PATH is required when SMS is enabled")
        return
    map_path = _resolve_path(config.voidfix_device_map_path, root)
    if map_path is None or not map_path.exists():
        report.add("error", "voidfix_map_missing", f"VoidFix device map not found: {map_path}")
        return
    try:
        vf_map = load_voidfix_device_map(map_path)
    except VoidFixDeviceMapError as exc:
        report.add("error", "voidfix_map_invalid", str(exc))
        return
    if config.voidfix_allowed_slot_ids:
        missing = [s for s in config.voidfix_allowed_slot_ids if s not in vf_map]
        if missing:
            report.add("error", "voidfix_allowlist_slots", f"allowed slots missing from device map: {missing}")
    elif len(vf_map) != expect_farm_slots:
        report.add(
            "warn",
            "voidfix_map_count",
            f"VoidFix map has {len(vf_map)} slots; expected {expect_farm_slots} when allowlist is empty",
        )


def _validate_sms_flags(config: AgentConfig, report: ProductionValidationReport) -> None:
    if not config.sms_enabled:
        return
    if not config.voidfix_recipient_allowlist:
        report.add(
            "warn",
            "voidfix_allowlist_empty",
            "VOIDFIX_RECIPIENT_ALLOWLIST is empty; outbound SMS will be blocked by policy",
        )
    if config.voidfix_dry_run:
        report.add("warn", "voidfix_dry_run", "VOIDFIX_DRY_RUN=true; live sends are simulated/blocked")
    if not config.voidfix_live_send_authorized:
        report.add(
            "warn",
            "voidfix_live_send",
            "VOIDFIX_LIVE_SEND_AUTHORIZED=false; live sends blocked unless dry_run handles them",
        )
