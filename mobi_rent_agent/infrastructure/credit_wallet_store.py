"""Local credit wallet and webhook idempotency (tests and VPS audit).

Production credit balances live in Lovable/Supabase. This store applies the
same grant/revoke math when no Lovable client is configured, and records
processed WHMCS event keys so retries do not double-credit.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from application.whmcs_webhook import ACTION_GRANT, ACTION_REVOKE

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class WalletSnapshot:
    user_id: str
    credits_available: int
    credits_total: int
    tier: str | None
    subscription_status: str
    replayed: bool = False


class CreditWalletStore:
    def __init__(self, db_path: Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._lock = threading.Lock()
        self._init_schema()

    @property
    def db_path(self) -> Path:
        return self._path

    def _init_schema(self) -> None:
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS credit_wallet_schema_version (version INTEGER NOT NULL)"
        )
        row = self._conn.execute("SELECT version FROM credit_wallet_schema_version").fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO credit_wallet_schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS credit_wallets (
                    user_id TEXT PRIMARY KEY,
                    credits_available INTEGER NOT NULL,
                    credits_total INTEGER NOT NULL,
                    tier TEXT,
                    subscription_status TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS credit_wallet_events (
                    idempotency_key TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            self._conn.commit()

    def get(self, user_id: str) -> WalletSnapshot | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT user_id, credits_available, credits_total, tier, subscription_status "
                "FROM credit_wallets WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return WalletSnapshot(
            user_id=str(row[0]),
            credits_available=int(row[1]),
            credits_total=int(row[2]),
            tier=str(row[3]) if row[3] else None,
            subscription_status=str(row[4]),
        )

    def apply(
        self,
        *,
        user_id: str,
        action: str,
        credit_delta: int,
        tier: str | None,
        subscription_status: str,
        idempotency_key: str,
        now: float | None = None,
    ) -> WalletSnapshot:
        stamp = time.time() if now is None else float(now)
        with self._lock:
            seen = self._conn.execute(
                "SELECT 1 FROM credit_wallet_events WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            current = self._conn.execute(
                "SELECT credits_available, credits_total, tier, subscription_status "
                "FROM credit_wallets WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if seen is not None:
                if current is None:
                    return WalletSnapshot(
                        user_id=user_id,
                        credits_available=0,
                        credits_total=0,
                        tier=tier,
                        subscription_status=subscription_status,
                        replayed=True,
                    )
                return WalletSnapshot(
                    user_id=user_id,
                    credits_available=int(current[0]),
                    credits_total=int(current[1]),
                    tier=str(current[2]) if current[2] else None,
                    subscription_status=str(current[3]),
                    replayed=True,
                )
            available = int(current[0]) if current else 0
            total = int(current[1]) if current else 0
            next_tier = tier if tier is not None else (str(current[2]) if current and current[2] else None)
            if action == ACTION_GRANT:
                available += int(credit_delta)
                total += int(credit_delta)
                status = subscription_status or "active"
            elif action == ACTION_REVOKE:
                available = max(0, available - int(credit_delta))
                status = subscription_status or "suspended"
            else:
                status = subscription_status or (str(current[3]) if current else "active")
            self._conn.execute(
                """
                INSERT INTO credit_wallets
                    (user_id, credits_available, credits_total, tier, subscription_status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    credits_available = excluded.credits_available,
                    credits_total = excluded.credits_total,
                    tier = excluded.tier,
                    subscription_status = excluded.subscription_status,
                    updated_at = excluded.updated_at
                """,
                (user_id, available, total, next_tier, status, stamp),
            )
            self._conn.execute(
                """
                INSERT INTO credit_wallet_events (idempotency_key, user_id, action, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (idempotency_key, user_id, action, stamp),
            )
            self._conn.commit()
        return WalletSnapshot(
            user_id=user_id,
            credits_available=available,
            credits_total=total,
            tier=next_tier,
            subscription_status=status,
        )

    def close(self) -> None:
        self._conn.close()
