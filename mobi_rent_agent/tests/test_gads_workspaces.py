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


def test_twenty_unique_workspaces_are_kept():
    mapping = {bay: f"ws-{bay:02d}" for bay in range(1, 21)}
    cleaned = unique_workspace_map(mapping)
    assert cleaned == mapping
    assert len(set(cleaned.values())) == 20


def test_slot_21_workspace_is_kept():
    mapping = {bay: f"ws-{bay:02d}" for bay in range(1, 22)}
    cleaned = unique_workspace_map(mapping)
    assert cleaned[21] == "ws-21"
    assert len(cleaned) == 21


def test_merge_uses_slot1_fallback_when_file_omits_bay1():
    merged = merge_workspace_map({8: "ws-8"}, fallback_slot1="ws-1")
    assert merged == {1: "ws-1", 8: "ws-8"}


def test_merge_does_not_let_slot8_reuse_slot1_workspace():
    merged = merge_workspace_map({8: "ws-1"}, fallback_slot1="ws-1")
    assert merged == {}


def test_load_gads_workspace_map(tmp_path: Path):
    path = tmp_path / "gads_workspaces.json"
    path.write_text(json.dumps({"1": "ws-a", "8": "ws-b", "21": "ws-s20"}), encoding="utf-8")
    assert load_gads_workspace_map(path) == {1: "ws-a", 8: "ws-b", 21: "ws-s20"}


def test_example_template_covers_every_rentable_bay():
    """The shipped template must include bay 21 (Samsung S20): a bay missing
    from gads_workspaces.json returns phone_unavailable on every remote-access
    route even when the GADS hub has the device registered."""
    example = Path(__file__).resolve().parents[1] / "gads_workspaces.example.json"
    mapping = load_gads_workspace_map(example)
    assert sorted(mapping) == list(range(1, 22))
    assert len(set(mapping.values())) == 21  # placeholders stay unique per bay


def test_missing_workspace_map_path_is_empty():
    assert load_gads_workspace_map(None) == {}


def test_missing_workspace_map_file_raises(tmp_path: Path):
    with pytest.raises(GadsWorkspaceMapError):
        load_gads_workspace_map(tmp_path / "missing.json")


def test_resolve_falls_back_to_gads_secrets_dir(tmp_path: Path, monkeypatch):
    from infrastructure.gads_workspaces import resolve_gads_workspace_map

    missing = tmp_path / "missing.json"
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "gads_workspaces.json").write_text(
        json.dumps({"21": "ws-s20", "1": "ws-one"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("GADS_SECRETS_DIR", str(secrets))
    mapping = resolve_gads_workspace_map(missing)
    assert mapping[21] == "ws-s20"
    assert mapping[1] == "ws-one"
