#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
from pathlib import Path


DB = Path("data/reliability_reports.sqlite3")


def fail(message: str) -> None:
    raise SystemExit(f"❌ {message}")


def main() -> None:
    print("🧪 Verifying agent learning store")

    if not DB.exists():
        fail(f"{DB} not found")

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    tables = {
        row["name"]
        for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }

    if "root_cause_learnings" not in tables:
        fail("root_cause_learnings table missing")

    if "reliability_reports" not in tables:
        fail("reliability_reports table missing")

    cols = {
        row["name"]
        for row in con.execute("PRAGMA table_info(root_cause_learnings)")
    }

    required_cols = {
        "id",
        "created_at",
        "last_seen_at",
        "root_cause",
        "signature",
        "successful_repair_count",
        "failed_repair_count",
        "last_repair_action",
        "last_verification_level",
        "notes",
        "evidence_sample",
    }

    missing = sorted(required_cols - cols)
    if missing:
        fail(f"root_cause_learnings missing columns: {missing}")

    # Verify duplicate signature can be updated safely.
    signature = "verify_agent_learning_store_local_signature"
    root_cause = "verify_agent_learning_store_local"
    now = "2026-06-13T00:00:00Z"

    con.execute(
        """
        INSERT INTO root_cause_learnings (
            created_at,
            last_seen_at,
            root_cause,
            signature,
            successful_repair_count,
            failed_repair_count,
            last_repair_action,
            last_verification_level,
            notes,
            evidence_sample
        )
        VALUES (?, ?, ?, ?, 1, 0, ?, ?, ?, ?)
        ON CONFLICT(signature) DO UPDATE SET
            last_seen_at=excluded.last_seen_at,
            successful_repair_count=root_cause_learnings.successful_repair_count + 1,
            last_repair_action=excluded.last_repair_action,
            last_verification_level=excluded.last_verification_level,
            notes=excluded.notes,
            evidence_sample=excluded.evidence_sample
        """,
        (
            now,
            now,
            root_cause,
            signature,
            "verify_upsert",
            "local",
            "verifier first insert/update",
            "first sample",
        ),
    )

    con.execute(
        """
        INSERT INTO root_cause_learnings (
            created_at,
            last_seen_at,
            root_cause,
            signature,
            successful_repair_count,
            failed_repair_count,
            last_repair_action,
            last_verification_level,
            notes,
            evidence_sample
        )
        VALUES (?, ?, ?, ?, 1, 0, ?, ?, ?, ?)
        ON CONFLICT(signature) DO UPDATE SET
            last_seen_at=excluded.last_seen_at,
            successful_repair_count=root_cause_learnings.successful_repair_count + 1,
            last_repair_action=excluded.last_repair_action,
            last_verification_level=excluded.last_verification_level,
            notes=excluded.notes,
            evidence_sample=excluded.evidence_sample
        """,
        (
            now,
            now,
            root_cause,
            signature,
            "verify_upsert_second",
            "local",
            "verifier duplicate update",
            "second sample",
        ),
    )

    con.commit()

    row = con.execute(
        "SELECT * FROM root_cause_learnings WHERE signature=?",
        (signature,),
    ).fetchone()

    if not row:
        fail("upsert verifier row missing")

    if int(row["successful_repair_count"] or 0) < 2:
        fail("duplicate upsert did not increment successful_repair_count")

    print("✅ root_cause_learnings schema present")
    print("✅ duplicate signature upsert works")
    print("✅ AGENT LEARNING STORE LOCAL VERIFICATION PASSED")

    con.close()


if __name__ == "__main__":
    main()
