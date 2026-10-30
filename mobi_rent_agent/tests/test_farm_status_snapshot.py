"""Warm in-memory farm status snapshot (admin list <100ms)."""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infrastructure.farm_status_snapshot import FarmStatusSnapshot
from tools.farm_agent_status_server import Handler as FarmHandler


def test_snapshot_get_is_memory_after_warmup():
    calls = {"n": 0}

    def builder():
        calls["n"] += 1
        if calls["n"] > 1:
            time.sleep(0.25)
        return {"ok": True, "offline_slots": [], "mapped_slots": [1, 21]}

    snap = FarmStatusSnapshot(builder, interval_seconds=60)
    snap.start()
    try:
        t0 = time.perf_counter()
        body = snap.get()
        elapsed_ms = (time.perf_counter() - t0) * 1000
        assert body["ok"] is True
        assert body["mapped_slots"] == [1, 21]
        assert elapsed_ms < 100
        assert calls["n"] == 1
        again = snap.get()
        assert again["mapped_slots"] == [1, 21]
        assert calls["n"] == 1
    finally:
        snap.stop()


def test_snapshot_keeps_last_good_body_when_refresh_fails():
    state = {"n": 0}

    def builder():
        state["n"] += 1
        if state["n"] == 1:
            return {"ok": True, "offline_slots": [2]}
        raise RuntimeError("farm down")

    snap = FarmStatusSnapshot(builder, interval_seconds=60)
    snap.start()
    try:
        assert snap.get()["offline_slots"] == [2]
        snap.refresh_once()
        assert snap.get()["offline_slots"] == [2]
        assert snap.get()["ok"] is True
    finally:
        snap.stop()


def test_farm_health_http_serves_warm_snapshot():
    builds = {"n": 0}

    def builder():
        builds["n"] += 1
        time.sleep(0.05)
        return {
            "ok": True,
            "role": "farm",
            "mapped_slots": [21],
            "offline_slots": [],
            "adb_online": 1,
        }

    snap = FarmStatusSnapshot(builder, interval_seconds=60)
    snap.start()
    FarmHandler.api_token = "secret"
    FarmHandler.adb_path = "adb-missing-for-test"
    FarmHandler.slot_map = {21: "RFCNB056WEV"}
    FarmHandler.status_snapshot = snap
    server = ThreadingHTTPServer(("127.0.0.1", 0), FarmHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import urllib.request

        def call():
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/agent/health",
                headers={"Authorization": "Bearer secret"},
                method="GET",
            )
            t0 = time.perf_counter()
            with urllib.request.urlopen(req, timeout=3) as resp:
                body = json.loads(resp.read())
            return resp.status, body, (time.perf_counter() - t0) * 1000

        status, body, first_ms = call()
        assert status == 200 and body["mapped_slots"] == [21]
        status, body, second_ms = call()
        assert status == 200 and body["ok"] is True
        assert builds["n"] == 1
        assert second_ms < 100
        assert first_ms < 100
    finally:
        server.shutdown()
        snap.stop()
        FarmHandler.status_snapshot = None
