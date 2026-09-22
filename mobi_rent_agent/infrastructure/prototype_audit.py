"""Prototype audit records. No secrets, activation codes, or SMS bodies."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from domain.prototype import redact_msisdn


class PrototypeAuditStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def get(self, operation_id: str) -> dict[str, Any] | None:
        for record in self._iter():
            if record.get("operation_id") == operation_id:
                return record
        return None

    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        cleaned = _sanitize(record)
        if "created_at" not in cleaned:
            cleaned["created_at"] = _now()
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(cleaned, sort_keys=True) + "\n")
        return cleaned

    def _iter(self):
        if not self._path.exists():
            return
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


_FORBIDDEN = frozenset(
    {
        "api_key",
        "authorization",
        "activation_code",
        "message",
        "sms_body",
        "token",
        "secret",
        "voidfix_api_key",
        "webhook_secret",
    }
)


def _sanitize(record: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in record.items():
        lowered = key.lower()
        if lowered in _FORBIDDEN or lowered.endswith("_api_key") or lowered.endswith("_secret"):
            continue
        if key in {"recipient", "to_number"} and isinstance(value, str):
            cleaned[key] = redact_msisdn(value)
            continue
        cleaned[key] = value
    return cleaned
