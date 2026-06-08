from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB = Path("data/reliability_reports.sqlite3")


def ensure_columns(conn: sqlite3.Connection) -> None:
    conn.execute("""
    CREATE TABLE IF NOT EXISTS reliability_knowledge (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pattern_id TEXT UNIQUE
    )
    """)

    required = {
        "seen_count": "INTEGER DEFAULT 1",
        "repair_success_count": "INTEGER DEFAULT 0",
        "repair_failed_count": "INTEGER DEFAULT 0",
        "last_seen_utc": "TEXT",
        "archived": "INTEGER DEFAULT 0",
    }

    existing = {row[1] for row in conn.execute("PRAGMA table_info(reliability_knowledge)")}
    for col, typ in required.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE reliability_knowledge ADD COLUMN {col} {typ}")


def compact() -> None:
    conn = sqlite3.connect(DB)
    ensure_columns(conn)

    now = datetime.now(timezone.utc).isoformat()

    # Backfill counters.
    conn.execute("""
        UPDATE reliability_knowledge
        SET seen_count = COALESCE(seen_count, 1),
            last_seen_utc = COALESCE(last_seen_utc, created_at_utc, ?)
    """, (now,))

    # Archive very old safe lessons that have already been converted into deterministic rules.
    deterministic_patterns = [
        "hosted_planner_runtime_mismatch",
        "fastapi_planner_not_running",
        "algorithm_changed_but_single_upstream",
        "bare_metal_ssh_cleanup_self_kill",
        "local_docker_edge_active_mismatch",
    ]

    for pattern in deterministic_patterns:
        conn.execute("""
            UPDATE reliability_knowledge
            SET archived = 1
            WHERE pattern_id = ?
        """, (pattern,))

    conn.commit()

    print("Reliability knowledge compacted.")
    print("Archived deterministic lessons:")
    for row in conn.execute("""
        SELECT pattern_id, archived, seen_count, last_seen_utc
        FROM reliability_knowledge
        WHERE archived = 1
        ORDER BY pattern_id
    """):
        print(row)

    conn.close()


if __name__ == "__main__":
    compact()
