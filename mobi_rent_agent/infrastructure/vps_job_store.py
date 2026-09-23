"""Persistent VPS background jobs (assign, device actions)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 1


@dataclass
class VpsJobRecord:
    job_id: str
    type: str
    farm_slot_id: int | None
    status: str
    progress: int
    request_payload: dict
    result_payload: dict | None
    error: str | None
    idempotency_key: str | None
    created_at: float
    updated_at: float
    started_at: float | None
    completed_at: float | None


class VpsJobStore:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        tables = {
            row[0]
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "vps_job_schema_version" not in tables:
            self._conn.execute(
                "CREATE TABLE vps_job_schema_version (version INTEGER NOT NULL)"
            )
            self._conn.execute(
                "INSERT INTO vps_job_schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            self._conn.execute(
                """
                CREATE TABLE vps_jobs (
                    job_id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    farm_slot_id INTEGER,
                    status TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    request_payload TEXT NOT NULL,
                    result_payload TEXT,
                    error TEXT,
                    idempotency_key TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    completed_at REAL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX idx_vps_jobs_status ON vps_jobs (status, created_at)"
            )
            self._conn.execute(
                "CREATE INDEX idx_vps_jobs_slot ON vps_jobs (farm_slot_id, status)"
            )
            self._conn.execute(
                "CREATE UNIQUE INDEX idx_vps_jobs_idempotency "
                "ON vps_jobs (type, farm_slot_id, idempotency_key) "
                "WHERE idempotency_key IS NOT NULL"
            )
            self._conn.commit()

    def create(
        self,
        *,
        job_type: str,
        farm_slot_id: int | None,
        request_payload: dict,
        idempotency_key: str | None = None,
    ) -> VpsJobRecord:
        job_id = str(uuid.uuid4())
        now = time.time()
        record = VpsJobRecord(
            job_id=job_id,
            type=job_type,
            farm_slot_id=farm_slot_id,
            status="pending",
            progress=0,
            request_payload=request_payload,
            result_payload=None,
            error=None,
            idempotency_key=idempotency_key,
            created_at=now,
            updated_at=now,
            started_at=None,
            completed_at=None,
        )
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO vps_jobs (
                        job_id, type, farm_slot_id, status, progress,
                        request_payload, result_payload, error, idempotency_key,
                        created_at, updated_at, started_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.job_id,
                        record.type,
                        record.farm_slot_id,
                        record.status,
                        record.progress,
                        json.dumps(request_payload),
                        None,
                        None,
                        idempotency_key,
                        now,
                        now,
                        None,
                        None,
                    ),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                self._conn.rollback()
                if idempotency_key is None or farm_slot_id is None:
                    raise
                existing = self.get_by_idempotency(job_type, farm_slot_id, idempotency_key)
                if existing is None:
                    raise
                return existing
        return record

    def get(self, job_id: str) -> VpsJobRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM vps_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return _row_to_record(row) if row else None

    def get_by_idempotency(
        self, job_type: str, farm_slot_id: int, idempotency_key: str
    ) -> VpsJobRecord | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM vps_jobs
                WHERE type = ? AND farm_slot_id = ? AND idempotency_key = ?
                """,
                (job_type, farm_slot_id, idempotency_key),
            ).fetchone()
        return _row_to_record(row) if row else None

    def has_active_job_for_slot(self, farm_slot_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1 FROM vps_jobs
                WHERE farm_slot_id = ? AND status IN ('pending', 'running')
                LIMIT 1
                """,
                (farm_slot_id,),
            ).fetchone()
        return row is not None

    def list_pending(self, limit: int = 20) -> list[VpsJobRecord]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM vps_jobs
                WHERE status = 'pending'
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    def update(
        self,
        job_id: str,
        *,
        status: str | None = None,
        progress: int | None = None,
        result_payload: dict | None = None,
        error: str | None = None,
        started_at: float | None = None,
        completed_at: float | None = None,
    ) -> None:
        now = time.time()
        fields = ["updated_at = ?"]
        values: list[object] = [now]
        if status is not None:
            fields.append("status = ?")
            values.append(status)
        if progress is not None:
            fields.append("progress = ?")
            values.append(progress)
        if result_payload is not None:
            fields.append("result_payload = ?")
            values.append(json.dumps(result_payload))
        if error is not None:
            fields.append("error = ?")
            values.append(error)
        if started_at is not None:
            fields.append("started_at = ?")
            values.append(started_at)
        if completed_at is not None:
            fields.append("completed_at = ?")
            values.append(completed_at)
        values.append(job_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE vps_jobs SET {', '.join(fields)} WHERE job_id = ?",
                values,
            )
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def _row_to_record(row: sqlite3.Row) -> VpsJobRecord:
    result = row["result_payload"]
    return VpsJobRecord(
        job_id=row["job_id"],
        type=row["type"],
        farm_slot_id=int(row["farm_slot_id"]) if row["farm_slot_id"] is not None else None,
        status=row["status"],
        progress=int(row["progress"]),
        request_payload=json.loads(row["request_payload"]),
        result_payload=json.loads(result) if result else None,
        error=row["error"],
        idempotency_key=row["idempotency_key"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        started_at=float(row["started_at"]) if row["started_at"] is not None else None,
        completed_at=float(row["completed_at"]) if row["completed_at"] is not None else None,
    )
