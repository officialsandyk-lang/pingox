from __future__ import annotations

import json
import sqlite3
from datetime import datetime, UTC
from pathlib import Path
from typing import Any


DB_PATH = Path("data/reliability_reports.sqlite3")


def _ensure_knowledge_table() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS reliability_knowledge (
            pattern_id TEXT PRIMARY KEY,
            created_at_utc TEXT NOT NULL,
            category TEXT NOT NULL,
            root_cause TEXT NOT NULL,
            summary TEXT NOT NULL,
            repair_hint TEXT NOT NULL,
            markers_json TEXT NOT NULL
        )
        """
    )

    conn.commit()
    conn.close()


def _upsert_knowledge(
    pattern_id: str,
    category: str,
    root_cause: str,
    summary: str,
    repair_hint: str,
    markers: list[str],
) -> None:
    _ensure_knowledge_table()

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute(
        """
        INSERT OR REPLACE INTO reliability_knowledge
        (pattern_id, created_at_utc, category, root_cause, summary, repair_hint, markers_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pattern_id,
            datetime.now(UTC).isoformat(),
            category,
            root_cause,
            summary,
            repair_hint,
            json.dumps(markers),
        ),
    )

    conn.commit()
    conn.close()


def run_root_cause_learning_node(state: dict[str, Any]) -> dict[str, Any]:
    """
    Converts deterministic decision failures into root-cause memory.
    This makes the self-healing system learn from logical/config failures too.
    """

    decision = state.get("decision_result") or {}
    failed = decision.get("failed_decisions") or []

    learned = []

    for item in failed:
        category = item.get("category")
        reason = item.get("reason", "")
        evidence = item.get("evidence") or {}

        if category == "weighted_round_robin_integrity":
            _upsert_knowledge(
                pattern_id="weighted_route_weight_loss_v1",
                category="config_algorithm_integrity",
                root_cause="nlp_weighted_route_weight_loss",
                summary="Weighted round-robin route lost intended weights, duplicated upstreams, or mixed string/dict upstream shape.",
                repair_hint="Preserve weighted upstream objects, reject mixed upstream shape, and use deterministic config-file baseline for algorithm stress tests.",
                markers=[
                    "weighted_round_robin_integrity",
                    "weighted_round_robin ratio failed",
                    "duplicate upstream",
                    "weights became 1:1:1",
                    "mixed string/dict upstream shape",
                ],
            )
            learned.append("nlp_weighted_route_weight_loss")

        if category == "runtime_addressing":
            _upsert_knowledge(
                pattern_id="runtime_addressing_leak_v1",
                category="runtime_addressing",
                root_cause="runtime_addressing_leak",
                summary="Local/Docker upstream addressing leaked across runtimes.",
                repair_hint="Rewrite local upstreams to 127.0.0.1 and Docker upstreams to host.docker.internal before writing project.",
                markers=[
                    "host.docker.internal in local",
                    "127.0.0.1 in docker_host",
                    "runtime_addressing",
                ],
            )
            learned.append("runtime_addressing_leak")

        if category == "route_mutation_safety":
            _upsert_knowledge(
                pattern_id="route_mutation_confirmation_missing_v1",
                category="deployment_safety",
                root_cause="route_mutation_confirmation_missing",
                summary="Route edit/delete/remove was blocked because explicit mutation confirmation was missing.",
                repair_hint='Re-run mutation with phrase "CONFIRM ROUTE MUTATION".',
                markers=[
                    "CONFIRM ROUTE MUTATION",
                    "route mutation warning",
                    "No changes were deployed",
                ],
            )
            learned.append("route_mutation_confirmation_missing")

    state["root_cause_learned"] = learned
    return state
