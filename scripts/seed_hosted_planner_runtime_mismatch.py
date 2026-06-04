from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB = Path("data/reliability_reports.sqlite3")
DB.parent.mkdir(parents=True, exist_ok=True)

REQUIRED_COLUMNS = {
    "pattern_id": "TEXT UNIQUE",
    "root_cause": "TEXT",
    "category": "TEXT",
    "summary": "TEXT",
    "repair_hint": "TEXT",
    "repair_action": "TEXT",
    "safe_to_auto_repair": "INTEGER DEFAULT 0",
    "markers_json": "TEXT",
    "created_at_utc": "TEXT",
}


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
    CREATE TABLE IF NOT EXISTS reliability_knowledge (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pattern_id TEXT UNIQUE
    )
    """)

    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info(reliability_knowledge)")
    }

    for column, column_type in REQUIRED_COLUMNS.items():
        if column not in existing:
            conn.execute(
                f"ALTER TABLE reliability_knowledge ADD COLUMN {column} {column_type}"
            )


def main() -> None:
    conn = sqlite3.connect(DB)
    ensure_table(conn)

    pattern_id = "hosted_planner_runtime_mismatch"

    markers = {
        "markers": [
            "pingora-ai plan --local returned docker_host",
            "runtime mismatch",
            "hosted planner returned docker_host despite CLI --local",
            "strategy bluegreen returned for local direct request",
            "CLI runtime flag ignored by plan",
        ],
        "expected_decision": {
            "--local": {"runtime": "local", "strategy": "direct"},
            "--docker": {"runtime": "docker_host", "strategy": "bluegreen"},
            "--linux": {"runtime": "bare_metal_linux", "strategy": "bluegreen"},
        },
        "repair_action": "enforce_cli_requested_runtime_strategy",
        "safe_to_auto_repair": True,
    }

    conn.execute(
        """
        INSERT INTO reliability_knowledge (
            pattern_id,
            root_cause,
            category,
            summary,
            repair_hint,
            repair_action,
            safe_to_auto_repair,
            markers_json,
            created_at_utc
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(pattern_id) DO UPDATE SET
            root_cause=excluded.root_cause,
            category=excluded.category,
            summary=excluded.summary,
            repair_hint=excluded.repair_hint,
            repair_action=excluded.repair_action,
            safe_to_auto_repair=excluded.safe_to_auto_repair,
            markers_json=excluded.markers_json,
            created_at_utc=excluded.created_at_utc
        """,
        (
            pattern_id,
            "hosted_planner_runtime_mismatch",
            "environment_selection",
            "Hosted planner returned docker_host/bluegreen even though the user CLI requested another runtime such as --local.",
            "Treat CLI runtime flags as source of truth. Enforce runtime/strategy after hosted planning and before saving/deploying config.",
            "enforce_cli_requested_runtime_strategy",
            1,
            json.dumps(markers, sort_keys=True),
            datetime.now(timezone.utc).isoformat(),
        ),
    )

    conn.commit()

    row = conn.execute(
        """
        SELECT pattern_id, root_cause, category, repair_action, safe_to_auto_repair
        FROM reliability_knowledge
        WHERE pattern_id = ?
        """,
        (pattern_id,),
    ).fetchone()

    conn.close()

    print("Seeded reliability lesson:")
    print(row)


if __name__ == "__main__":
    main()
