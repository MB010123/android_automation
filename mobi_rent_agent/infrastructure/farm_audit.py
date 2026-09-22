"""Append-only farm audit trail. Records reasons, never secrets."""
from __future__ import annotations

import json
import time
from pathlib import Path

DEFAULT_AUDIT_PATH = Path(__file__).resolve().parents[1] / "logs" / "farm_audit.jsonl"

_FORBIDDEN = (
    "api_key",
    "token",
    "password",
    "activation",
    "imei",
    "msisdn",
    "serial",
    "number",
    "secret",
    "key",
)


def append_farm_audit(event: dict, path: str | Path | None = None) -> None:
    """Write one JSON line. Drops keys that look like secrets or identifiers."""
    safe = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    for key, value in event.items():
        lowered = str(key).lower()
        if any(part in lowered for part in _FORBIDDEN):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[key] = value
        elif isinstance(value, (list, tuple)):
            safe[key] = [item for item in value if isinstance(item, (str, int, float, bool))]
        else:
            safe[key] = str(type(value).__name__)
    target = Path(path) if path else DEFAULT_AUDIT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(safe, separators=(",", ":")) + "\n")
