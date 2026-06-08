from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "data" / "decision_integrity.sqlite3"


@dataclass
class DecisionIntegrityRecord:
    stage: str
    agent: str
    decision: str
    ok: bool
    severity: str
    reason: str
    route: str | None = None
    action: str | None = None
    runtime: str | None = None
    strategy: str | None = None
    evidence: dict[str, Any] | None = None


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS decision_integrity_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_unix_ms INTEGER NOT NULL,
            stage TEXT NOT NULL,
            agent TEXT NOT NULL,
            decision TEXT NOT NULL,
            ok INTEGER NOT NULL,
            severity TEXT NOT NULL,
            reason TEXT NOT NULL,
            route TEXT,
            action TEXT,
            runtime TEXT,
            strategy TEXT,
            evidence_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lesson_learning_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_unix_ms INTEGER NOT NULL,
            lesson_key TEXT NOT NULL,
            root_cause TEXT NOT NULL,
            summary TEXT NOT NULL,
            prevention_rule TEXT NOT NULL,
            evidence_json TEXT
        )
        """
    )
    conn.commit()
    return conn


def record_decision(record: DecisionIntegrityRecord) -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO decision_integrity_events (
                created_unix_ms,
                stage,
                agent,
                decision,
                ok,
                severity,
                reason,
                route,
                action,
                runtime,
                strategy,
                evidence_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(time.time() * 1000),
                record.stage,
                record.agent,
                record.decision,
                1 if record.ok else 0,
                record.severity,
                record.reason,
                record.route,
                record.action,
                record.runtime,
                record.strategy,
                json.dumps(record.evidence or {}, sort_keys=True),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def record_lesson(
    *,
    lesson_key: str,
    root_cause: str,
    summary: str,
    prevention_rule: str,
    evidence: dict[str, Any] | None = None,
) -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO lesson_learning_events (
                created_unix_ms,
                lesson_key,
                root_cause,
                summary,
                prevention_rule,
                evidence_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                int(time.time() * 1000),
                lesson_key,
                root_cause,
                summary,
                prevention_rule,
                json.dumps(evidence or {}, sort_keys=True),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def assert_no_rejected_decision_downgraded_to_warning(
    *,
    warnings: list[dict[str, Any]] | None,
    runtime: str,
    strategy: str,
) -> None:
    """
    Any system_decision_engine rejection must be fatal.

    This prevents the old Linux bug where:
      rejected config -> warning -> deployment still passed.
    """
    for item in warnings or []:
        if not isinstance(item, dict):
            continue

        stage = str(item.get("stage") or item.get("source") or "").strip().lower()
        warning = str(item.get("warning") or "").strip()

        if "system_decision" in stage and "rejected config" in warning.lower():
            record_decision(
                DecisionIntegrityRecord(
                    stage=stage,
                    agent="system_decision_engine",
                    decision="reject",
                    ok=False,
                    severity="critical",
                    reason=warning,
                    runtime=runtime,
                    strategy=strategy,
                    evidence={"warning": item},
                )
            )

            record_lesson(
                lesson_key="rejected_decision_must_not_be_warning",
                root_cause="decision_rejection_downgraded_to_warning",
                summary="A system decision rejection was stored as a warning, allowing deployment to continue.",
                prevention_rule="Treat all system_decision_engine rejected config outputs as fatal before deploy.",
                evidence={"runtime": runtime, "strategy": strategy, "warning": item},
            )

            raise RuntimeError(warning)


def validate_algorithm_decision(
    *,
    route: str,
    algorithm: str,
    upstream_count: int,
    runtime: str,
    strategy: str,
    stage: str = "decision_integrity",
) -> None:
    multi_algorithms = {
        "random",
        "round_robin",
        "weighted_round_robin",
        "ip_hash",
        "least_connections",
    }

    algorithm = str(algorithm or "").strip().lower()

    if upstream_count <= 1 and algorithm in multi_algorithms:
        reason = (
            f"Cannot set {route} algorithm to {algorithm}: route has only "
            f"{upstream_count} upstream. Add at least two upstreams first."
        )

        record_decision(
            DecisionIntegrityRecord(
                stage=stage,
                agent="decision_integrity",
                decision="reject",
                ok=False,
                severity="critical",
                reason=reason,
                route=route,
                action="edit_algorithm",
                runtime=runtime,
                strategy=strategy,
                evidence={
                    "route": route,
                    "algorithm": algorithm,
                    "upstream_count": upstream_count,
                },
            )
        )

        record_lesson(
            lesson_key="multi_algorithm_requires_multiple_upstreams",
            root_cause="invalid_algorithm_for_single_upstream_route",
            summary="A multi-upstream load-balancing algorithm was requested for a one-upstream route.",
            prevention_rule="Block random/round_robin/weighted_round_robin/ip_hash/least_connections unless upstream_count >= 2.",
            evidence={
                "route": route,
                "algorithm": algorithm,
                "upstream_count": upstream_count,
                "runtime": runtime,
                "strategy": strategy,
            },
        )

        raise RuntimeError(reason)

    record_decision(
        DecisionIntegrityRecord(
            stage=stage,
            agent="decision_integrity",
            decision="allow",
            ok=True,
            severity="info",
            reason=f"Algorithm decision allowed for {route}.",
            route=route,
            action="edit_algorithm",
            runtime=runtime,
            strategy=strategy,
            evidence={
                "route": route,
                "algorithm": algorithm,
                "upstream_count": upstream_count,
            },
        )
    )


def latest_lessons(limit: int = 20) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT created_unix_ms, lesson_key, root_cause, summary, prevention_rule, evidence_json
            FROM lesson_learning_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        return [
            {
                "created_unix_ms": row[0],
                "lesson_key": row[1],
                "root_cause": row[2],
                "summary": row[3],
                "prevention_rule": row[4],
                "evidence": json.loads(row[5] or "{}"),
            }
            for row in rows
        ]
    finally:
        conn.close()


def latest_decisions(limit: int = 20) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT created_unix_ms, stage, agent, decision, ok, severity, reason,
                   route, action, runtime, strategy, evidence_json
            FROM decision_integrity_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        return [
            {
                "created_unix_ms": row[0],
                "stage": row[1],
                "agent": row[2],
                "decision": row[3],
                "ok": bool(row[4]),
                "severity": row[5],
                "reason": row[6],
                "route": row[7],
                "action": row[8],
                "runtime": row[9],
                "strategy": row[10],
                "evidence": json.loads(row[11] or "{}"),
            }
            for row in rows
        ]
    finally:
        conn.close()
