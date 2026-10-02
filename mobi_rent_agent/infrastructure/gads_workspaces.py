"""Bay -> GADS workspace id. One workspace per Farm bay.

A rental user is added only to their bay's workspace. Sharing one workspace
across bays is rejected so hub-ui cannot list sibling phones.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("vps_backend.gads_workspaces")


class GadsWorkspaceMapError(ValueError):
    """Workspace map file is missing or invalid."""


def load_gads_workspace_map(path: str | Path | None) -> dict[int, str]:
    """Load slot_id -> workspace UUID. Missing path yields an empty map."""
    if path is None or not str(path).strip():
        return {}
    file_path = Path(path)
    if not file_path.exists():
        raise GadsWorkspaceMapError(f"GADS workspace map not found: {file_path}")
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GadsWorkspaceMapError(f"Invalid GADS workspace map {file_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise GadsWorkspaceMapError("GADS workspace map must be a JSON object keyed by bay")
    mapping: dict[int, str] = {}
    for key, value in raw.items():
        try:
            slot_id = int(key)
        except (TypeError, ValueError) as exc:
            raise GadsWorkspaceMapError("GADS workspace map keys must be bay numbers 1-20") from exc
        if not 1 <= slot_id <= 20:
            raise GadsWorkspaceMapError(f"GADS workspace map bay must be 1-20, got {slot_id}")
        text = str(value or "").strip()
        if not text:
            continue
        if slot_id in mapping:
            raise GadsWorkspaceMapError(f"duplicate GADS workspace map bay {slot_id}")
        mapping[slot_id] = text
    return mapping


def unique_workspace_map(mapping: dict[int, str]) -> dict[int, str]:
    """Keep only bays whose workspace id is used by exactly one bay."""
    by_workspace: dict[str, list[int]] = {}
    for slot_id, workspace_id in mapping.items():
        text = str(workspace_id or "").strip()
        if not text:
            continue
        bay = int(slot_id)
        if not 1 <= bay <= 20:
            continue
        by_workspace.setdefault(text, []).append(bay)
    unique: dict[int, str] = {}
    for workspace_id, bays in by_workspace.items():
        if len(bays) != 1:
            logger.warning(
                "gads_workspace_shared_rejected workspace_bays=%s",
                ",".join(str(b) for b in sorted(bays)),
            )
            continue
        unique[bays[0]] = workspace_id
    return unique


def merge_workspace_map(
    file_map: dict[int, str],
    *,
    fallback_slot1: str | None,
) -> dict[int, str]:
    """File wins per bay. Slot 1 may use REMOTE_ACCESS_WORKSPACE_ID when absent."""
    combined = dict(file_map)
    fallback = str(fallback_slot1 or "").strip()
    if fallback and 1 not in combined:
        combined[1] = fallback
    return unique_workspace_map(combined)
