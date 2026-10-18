"""Local mirror of Supabase ``user_credits`` (tests / VPS fallback).

Production writes go through Lovable into ``public.user_credits``. This store
uses the same column names and grant/revoke/deduct/refund math.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from application.user_credits_contract import (
    COL_AVAILABLE,
    COL_TIER,
    COL_TOTAL,
    COL_USER_ID,
    COL_WHMCS_SERVICE_ID,
)
from application.whmcs_webhook import ACTION_GRANT, ACTION_REVOKE

SCHEMA_VERSION = 2
ACTION_DEDUCT = "credit_deduct"
ACTION_REFUND = "credit_refund"


@dataclass(frozen=True)
class WalletSnapshot:
    user_id: str
    available_credits: int
    total_credits: int
    subscription_tier: str | None
    whmcs_service_id: str | None = None
    subscription_status: str = "active"
    replayed: bool = False

    # Compatibility aliases for older call sites.
    @property
    def credits_available(self) -> int:
        return self.available_credits

    @property
    def credits_total(self) -> int:
        return self.total_credits

    @property
    def tier(self) -> str | None:
        return self.subscription_tier


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
        version = int(row[0]) if row else 0
        if version < 1:
            self._conn.execute(
                "INSERT INTO credit_wallet_schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            version = 0
        if version < 2:
            self._conn.execute("DROP TABLE IF EXISTS credit_wallets")
            self._conn.execute("DROP TABLE IF EXISTS credit_wallet_events")
            self._conn.execute(
                """
                CREATE TABLE credit_wallets (
                    user_id TEXT PRIMARY KEY,
                    available_credits INTEGER NOT NULL,
                    total_credits INTEGER NOT NULL,
                    subscription_tier TEXT,
                    whmcs_service_id TEXT,
                    subscription_status TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE credit_wallet_events (
                    idempotency_key TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                "UPDATE credit_wallet_schema_version SET version = ?",
                (SCHEMA_VERSION,),
            )
            self._conn.commit()

    def get(self, user_id: str) -> WalletSnapshot | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {COL_USER_ID}, {COL_AVAILABLE}, {COL_TOTAL}, {COL_TIER}, "
                f"{COL_WHMCS_SERVICE_ID}, subscription_status "
                "FROM credit_wallets WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return WalletSnapshot(
            user_id=str(row[0]),
            available_credits=int(row[1]),
            total_credits=int(row[2]),
            subscription_tier=str(row[3]) if row[3] else None,
            whmcs_service_id=str(row[4]) if row[4] else None,
            subscription_status=str(row[5]),
        )

    def apply(
        self,
        *,
        user_id: str,
        action: str,
        credit_delta: int,
        tier: str | None = None,
        subscription_tier: str | None = None,
        subscription_status: str = "active",
        whmcs_service_id: str | None = None,
        idempotency_key: str,
        now: float | None = None,
    ) -> WalletSnapshot:
        next_tier = subscription_tier if subscription_tier is not None else tier
        stamp = time.time() if now is None else float(now)
        with self._lock:
            seen = self._conn.execute(
                "SELECT 1 FROM credit_wallet_events WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            current = self._conn.execute(
                f"SELECT {COL_AVAILABLE}, {COL_TOTAL}, {COL_TIER}, {COL_WHMCS_SERVICE_ID}, "
                "subscription_status FROM credit_wallets WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if seen is not None:
                return self._snapshot_from_row(user_id, current, next_tier, subscription_status, replayed=True)
            available = int(current[0]) if current else 0
            total = int(current[1]) if current else 0
            stored_tier = str(current[2]) if current and current[2] else None
            stored_service = str(current[3]) if current and current[3] else None
            next_tier_value = next_tier if next_tier is not None else stored_tier
            next_service = whmcs_service_id if whmcs_service_id is not None else stored_service
            if action == ACTION_GRANT:
                available += int(credit_delta)
                total += int(credit_delta)
                status = subscription_status or "active"
            elif action == ACTION_REVOKE:
                available = max(0, available - int(credit_delta))
                status = subscription_status or "suspended"
            elif action == ACTION_DEDUCT:
                if available < int(credit_delta):
                    raise InsufficientCreditsError(available, int(credit_delta))
                available -= int(credit_delta)
                status = subscription_status or (str(current[4]) if current else "active")
            elif action == ACTION_REFUND:
                available += int(credit_delta)
                status = subscription_status or (str(current[4]) if current else "active")
            else:
                status = subscription_status or (str(current[4]) if current else "active")
            self._conn.execute(
                f"""
                INSERT INTO credit_wallets
                    ({COL_USER_ID}, {COL_AVAILABLE}, {COL_TOTAL}, {COL_TIER},
                     {COL_WHMCS_SERVICE_ID}, subscription_status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    {COL_AVAILABLE} = excluded.{COL_AVAILABLE},
                    {COL_TOTAL} = excluded.{COL_TOTAL},
                    {COL_TIER} = excluded.{COL_TIER},
                    {COL_WHMCS_SERVICE_ID} = excluded.{COL_WHMCS_SERVICE_ID},
                    subscription_status = excluded.subscription_status,
                    updated_at = excluded.updated_at
                """,
                (user_id, available, total, next_tier_value, next_service, status, stamp),
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
            available_credits=available,
            total_credits=total,
            subscription_tier=next_tier_value,
            whmcs_service_id=next_service,
            subscription_status=status,
        )

    def deduct(
        self,
        *,
        user_id: str,
        amount: int = 1,
        idempotency_key: str,
        now: float | None = None,
    ) -> WalletSnapshot:
        return self.apply(
            user_id=user_id,
            action=ACTION_DEDUCT,
            credit_delta=int(amount),
            subscription_status="active",
            idempotency_key=idempotency_key,
            now=now,
        )

    def refund(
        self,
        *,
        user_id: str,
        amount: int = 1,
        idempotency_key: str,
        now: float | None = None,
    ) -> WalletSnapshot:
        return self.apply(
            user_id=user_id,
            action=ACTION_REFUND,
            credit_delta=int(amount),
            subscription_status="active",
            idempotency_key=idempotency_key,
            now=now,
        )

    def _snapshot_from_row(
        self,
        user_id: str,
        current: tuple | None,
        tier: str | None,
        subscription_status: str,
        *,
        replayed: bool,
    ) -> WalletSnapshot:
        if current is None:
            return WalletSnapshot(
                user_id=user_id,
                available_credits=0,
                total_credits=0,
                subscription_tier=tier,
                subscription_status=subscription_status,
                replayed=replayed,
            )
        return WalletSnapshot(
            user_id=user_id,
            available_credits=int(current[0]),
            total_credits=int(current[1]),
            subscription_tier=str(current[2]) if current[2] else tier,
            whmcs_service_id=str(current[3]) if current[3] else None,
            subscription_status=str(current[4]) if len(current) > 4 else subscription_status,
            replayed=replayed,
        )

    def close(self) -> None:
        self._conn.close()


class InsufficientCreditsError(ValueError):
    def __init__(self, available: int, required: int) -> None:
        super().__init__("insufficient_credits")
        self.available = available
        self.required = required
