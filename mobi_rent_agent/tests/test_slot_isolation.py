from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.slot_isolation import SlotIsolationError, SlotIsolationPolicy

PRODUCTION_MAP = {slot: f"SERIAL-{slot}" for slot in range(1, 21)}
EXPECTED_SLOT_MAP_SHA256 = "86754037C135740CE0E32F71C0921C9213EDD3499764C86E87D5AA13DA641F89"
SLOT_MAP_PATH = Path(__file__).resolve().parents[1] / "slot_map.json"


def test_default_allowlist_is_slot_1():
    assert SlotIsolationPolicy().allowed_slot_ids == frozenset({1})


def test_production_map_plus_allowlist_1_claims_only_1():
    assert SlotIsolationPolicy({1}).claim_ids(PRODUCTION_MAP) == [1]


def test_production_map_plus_allowlist_1_2_claims_1_2():
    assert SlotIsolationPolicy({1, 2}).claim_ids(PRODUCTION_MAP) == [1, 2]


def test_empty_intersection_refuses_provisioning():
    policy = SlotIsolationPolicy({1})
    with pytest.raises(SlotIsolationError, match="empty"):
        policy.refuse_if_empty({9: "SERIAL-9"})


def test_slot_2_job_rejected_when_only_slot_1_allowed():
    policy = SlotIsolationPolicy({1})
    policy.reject_if_outside(1)
    with pytest.raises(SlotIsolationError, match="outside"):
        policy.reject_if_outside(2)


def test_claim_payload_never_includes_slots_2_to_20_when_allowlist_is_1():
    payload = SlotIsolationPolicy({1}).claim_payload(PRODUCTION_MAP)
    assert payload == {"slot_ids": [1]}
    assert payload["slot_ids"] == [1]


def test_worker_count_bounded_by_claim_set_not_map_size():
    assert SlotIsolationPolicy({1}).max_workers(20, PRODUCTION_MAP) == 1
    assert SlotIsolationPolicy({1, 2}).max_workers(20, PRODUCTION_MAP) == 2
    assert SlotIsolationPolicy({1, 2}).max_workers(1, PRODUCTION_MAP) == 1


def test_policy_does_not_use_map_keys_as_scope():
    policy = SlotIsolationPolicy({1})
    mapped_keys = set(PRODUCTION_MAP)
    assert set(policy.claim_ids(PRODUCTION_MAP)) != mapped_keys
    assert set(policy.claim_ids(PRODUCTION_MAP)) == {1}


def test_invalid_allowed_slot_is_rejected():
    with pytest.raises(SlotIsolationError):
        SlotIsolationPolicy({0})
    with pytest.raises(SlotIsolationError):
        SlotIsolationPolicy({21})


def test_allowlist_does_not_mutate_slot_map_json():
    if not SLOT_MAP_PATH.exists():
        pytest.skip("production slot_map.json is not present")
    before = SLOT_MAP_PATH.read_bytes()
    digest = hashlib.sha256(before).hexdigest().upper()
    assert digest == EXPECTED_SLOT_MAP_SHA256
    import json

    mapped = {int(k): v for k, v in json.loads(before.decode()).items()}
    assert len(mapped) == 20
    policy = SlotIsolationPolicy({1})
    assert policy.claim_ids(mapped) == [1]
    policy.claim_payload(mapped)
    after = SLOT_MAP_PATH.read_bytes()
    assert after == before
    assert hashlib.sha256(after).hexdigest().upper() == EXPECTED_SLOT_MAP_SHA256
