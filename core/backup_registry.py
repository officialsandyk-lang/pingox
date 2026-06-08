from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "data" / "backup_registry.sqlite3"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS backup_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_unix_ms INTEGER NOT NULL,
            backup_path TEXT NOT NULL,
            reason TEXT NOT NULL,
            runtime TEXT,
            strategy TEXT,
            route_change TEXT,
            valid_before_backup INTEGER NOT NULL,
            rollback_status TEXT,
            evidence_json TEXT
        )
        """
    )
    conn.commit()
    return conn


def record_backup(
    *,
    backup_path: str | Path,
    reason: str,
    runtime: str | None = None,
    strategy: str | None = None,
    route_change: str | None = None,
    valid_before_backup: bool = True,
    rollback_status: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO backup_events (
                created_unix_ms,
                backup_path,
                reason,
                runtime,
                strategy,
                route_change,
                valid_before_backup,
                rollback_status,
                evidence_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(time.time() * 1000),
                str(backup_path),
                reason,
                runtime,
                strategy,
                route_change,
                1 if valid_before_backup else 0,
                rollback_status,
                json.dumps(evidence or {}, sort_keys=True),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def latest_backups(limit: int = 20) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT created_unix_ms, backup_path, reason, runtime, strategy,
                   route_change, valid_before_backup, rollback_status, evidence_json
            FROM backup_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        return [
            {
                "created_unix_ms": row[0],
                "backup_path": row[1],
                "reason": row[2],
                "runtime": row[3],
                "strategy": row[4],
                "route_change": row[5],
                "valid_before_backup": bool(row[6]),
                "rollback_status": row[7],
                "evidence": json.loads(row[8] or "{}"),
            }
            for row in rows
        ]
    finally:
        conn.close()
