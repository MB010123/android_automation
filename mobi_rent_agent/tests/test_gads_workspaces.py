"""Unit tests for one-workspace-per-bay GADS mapping."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from infrastructure.gads_workspaces import (
    GadsWorkspaceMapError,
    load_gads_workspace_map,
    merge_workspace_map,
    unique_workspace_map,
)


def test_unique_workspace_map_drops_shared_ids():
    cleaned = unique_workspace_map({7: "shared", 8: "shared", 1: "solo"})
    assert cleaned == {1: "solo"}


def test_merge_uses_slot1_fallback_when_file_omits_bay1():
    merged = merge_workspace_map({8: "ws-8"}, fallback_slot1="ws-1")
    assert merged == {1: "ws-1", 8: "ws-8"}


def test_merge_does_not_let_slot8_reuse_slot1_workspace():
    merged = merge_workspace_map({8: "ws-1"}, fallback_slot1="ws-1")
    assert merged == {}


def test_load_gads_workspace_map(tmp_path: Path):
    path = tmp_path / "gads_workspaces.json"
    path.write_text(json.dumps({"1": "ws-a", "8": "ws-b"}), encoding="utf-8")
    assert load_gads_workspace_map(path) == {1: "ws-a", 8: "ws-b"}


def test_missing_workspace_map_path_is_empty():
    assert load_gads_workspace_map(None) == {}


def test_missing_workspace_map_file_raises(tmp_path: Path):
    with pytest.raises(GadsWorkspaceMapError):
        load_gads_workspace_map(tmp_path / "missing.json")
