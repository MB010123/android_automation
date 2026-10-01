"""Read-only Farm Agent cache of assign results, keyed by job_id.

Companion job cache is only consulted by sending provision_esim, which
forwards an activation code on a miss. This cache is the Farm-side query
surface used during VPS recovery. Activation codes are never stored.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from application.farm_task_types import FarmTaskResult
from application.install_state import INSTALL_FAILED, KEEP_ASSIGNMENT_STATES

_SECRET_KEYS = frozenset({"activation_code", "qr_url", "esim_qr_url", "image_base64"})


def _strip_secrets(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in _SECRET_KEYS}


def result_to_cache_body(job_id: str, result: FarmTaskResult) -> dict[str, Any]:
    body: dict[str, Any] = {
        "ok": bool(result.ok),
        "job_id": job_id,
        "error": result.error,
        "install_state": result.install_state,
        "activation_code_sent": bool(result.activation_code_sent),
        "found": True,
    }
    if result.message:
        body["message"] = result.message
    return _strip_secrets(body)


def should_reuse_assign_result(body: dict[str, Any] | None) -> bool:
    """True when a second assign for this job_id must not start another download."""
    if not isinstance(body, dict):
        return False
    state = str(body.get("install_state") or "")
    sent = bool(body.get("activation_code_sent"))
    if sent:
        return True
    if state in KEEP_ASSIGNMENT_STATES:
        return True
    return False


def is_authoritative_pre_send_failure(body: dict[str, Any] | None) -> bool:
    if not isinstance(body, dict):
        return False
    state = str(body.get("install_state") or "")
    sent = bool(body.get("activation_code_sent"))
    return state == INSTALL_FAILED and not sent


class FarmJobResultCache:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._rows: dict[str, dict[str, Any]] = {}
        if path is not None:
            self._load()

    def get(self, job_id: str) -> dict[str, Any] | None:
        key = str(job_id or "").strip()
        if not key:
            return None
        with self._lock:
            row = self._rows.get(key)
            return dict(row) if row else None

    def put(self, job_id: str, body: dict[str, Any]) -> dict[str, Any]:
        key = str(job_id or "").strip()
        stored = _strip_secrets(dict(body))
        stored["job_id"] = key
        stored["found"] = True
        if not key:
            return stored
        with self._lock:
            self._rows[key] = stored
            self._persist()
        return dict(stored)

    def remember_assign(self, job_id: str, result: FarmTaskResult) -> dict[str, Any]:
        return self.put(job_id, result_to_cache_body(job_id, result))

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            return
        if not isinstance(raw, dict):
            return
        for job_id, body in raw.items():
            if isinstance(body, dict):
                self._rows[str(job_id)] = _strip_secrets(body)

    def _persist(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._rows), encoding="utf-8")
