#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
from pathlib import Path


DB = Path("data/reliability_reports.sqlite3")


def main() -> None:
    if not DB.exists():
        raise SystemExit(f"❌ {DB} not found")

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    rows = con.execute(
        """
        SELECT
            root_cause,
            successful_repair_count,
            failed_repair_count,
            last_repair_action,
            last_verification_level,
            last_seen_at,
            notes
        FROM root_cause_learnings
        ORDER BY last_seen_at DESC
        LIMIT 20
        """
    ).fetchall()

    if not rows:
        print("No agent learnings found.")
        return

    print("Recent agent learnings:")
    for row in rows:
        print()
        print(f"- root_cause: {row['root_cause']}")
        print(f"  success: {row['successful_repair_count']} failed: {row['failed_repair_count']}")
        print(f"  action: {row['last_repair_action']}")
        print(f"  level: {row['last_verification_level']}")
        print(f"  last_seen: {row['last_seen_at']}")
        if row["notes"]:
            print(f"  notes: {row['notes']}")

    con.close()


if __name__ == "__main__":
    main()
