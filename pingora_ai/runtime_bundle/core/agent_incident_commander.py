from __future__ import annotations

import json
import time
import traceback
from pathlib import Path
from typing import Any, Callable


def now_id() -> str:
    return f"inc_{int(time.time())}"


def make_incident(
    *,
    runtime: str,
    strategy: str | None,
    operation: str,
    error: str,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "incident_id": now_id(),
        "runtime": runtime,
        "strategy": strategy,
        "operation": operation,
        "error": error,
        "config": config or {},
        "created_at": time.time(),
        "agent_findings": {},
        "root_cause_candidates": [],
        "selected_root_cause": None,
        "repair_plan": [],
        "repair_result": None,
        "verification": None,
        "recovered": False,
    }


def save_incident_snapshot(incident: dict[str, Any]) -> None:
    path = Path("data/reliability/agent_incidents.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(incident, sort_keys=True, default=str) + "\n")


def run_agent_task(
    *,
    incident: dict[str, Any],
    agent_name: str,
    task_fn: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    try:
        result = task_fn(incident)
        incident["agent_findings"][agent_name] = {
            "success": True,
            "result": result,
        }
    except Exception as exc:
        incident["agent_findings"][agent_name] = {
            "success": False,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }


def root_cause_task(incident: dict[str, Any]) -> dict[str, Any]:
    from agents.root_cause_agent import classify_from_reliability_knowledge

    diagnosis = classify_from_reliability_knowledge(
        runtime=incident["runtime"],
        operation=incident["operation"],
        error=incident["error"],
    )

    if diagnosis.get("matched"):
        incident["root_cause_candidates"].append(diagnosis)

    return diagnosis


def config_verification_task(incident: dict[str, Any]) -> dict[str, Any]:
    from agents.config_verification_agent import run_config_verification_agent

    config = incident.get("config") or {}

    return run_config_verification_agent(
        config=config,
        runtime=incident["runtime"],
        strategy=incident.get("strategy"),
        environment=incident["runtime"],
        live_url=config.get("live_url") or config.get("public_url") or config.get("base_url"),
        require_algorithm_integrity=False,
    )


def runtime_probe_task(incident: dict[str, Any]) -> dict[str, Any]:
    import socket
    import urllib.request

    runtime = incident["runtime"]
    checks: dict[str, Any] = {}

    if runtime == "local":
        checks["port_8090_open"] = _is_port_open("127.0.0.1", 8090)
        checks["homepage"] = _http_status("http://127.0.0.1:8090/")
        checks["api"] = _http_status("http://127.0.0.1:8090/api/")

    elif runtime == "docker_host":
        checks["edge_8090_open"] = _is_port_open("127.0.0.1", 8090)
        checks["tls_8443_open"] = _is_port_open("127.0.0.1", 8443)
        checks["edge_health"] = _http_status("http://127.0.0.1:8090/__edge_health")

    elif runtime in {"bare_metal_linux", "linux"}:
        checks["note"] = "Bare-metal runtime probe should be done through SSH health gate."

    return checks


def _is_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _http_status(url: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return {"status": response.status, "ok": 200 <= response.status < 400}
    except Exception as exc:
        return {"status": None, "ok": False, "error": str(exc)}


def select_root_cause(incident: dict[str, Any]) -> dict[str, Any]:
    candidates = incident.get("root_cause_candidates") or []

    if not candidates:
        return {
            "root_cause": "unknown_failure",
            "repair_action": None,
            "safe_to_auto_repair": False,
            "confidence": 0.0,
        }

    candidates = sorted(
        candidates,
        key=lambda item: float(item.get("confidence") or 0),
        reverse=True,
    )

    selected = candidates[0]
    incident["selected_root_cause"] = selected
    return selected


def build_repair_plan(incident: dict[str, Any]) -> list[dict[str, Any]]:
    root = incident.get("selected_root_cause") or {}
    action = root.get("repair_action")

    plan: list[dict[str, Any]] = []

    if action == "set_local_live_url_to_8090":
        plan.append({
            "step": "set_verifier_live_url",
            "runtime": "local",
            "value": "http://127.0.0.1:8090",
            "safe": True,
        })

    elif action == "set_local_readiness_to_homepage":
        plan.append({
            "step": "set_readiness_url",
            "runtime": "local",
            "value": "http://127.0.0.1:8090/",
            "safe": True,
        })

    elif action == "upload_static_assets_to_release":
        plan.append({
            "step": "ensure_bare_metal_static_assets_uploaded",
            "runtime": "bare_metal_linux",
            "safe": True,
        })

    elif action == "rewrite_docker_host_to_loopback":
        plan.append({
            "step": "rewrite_upstreams",
            "from": "host.docker.internal",
            "to": "127.0.0.1",
            "safe": True,
        })

    elif action == "kill_process_then_reset_failed":
        plan.append({
            "step": "systemd_kill_reset_failed",
            "runtime": "bare_metal_linux",
            "safe": True,
        })

    incident["repair_plan"] = plan
    return plan


def execute_repair_plan(incident: dict[str, Any]) -> dict[str, Any]:
    from core.environment_repairs import repair_from_diagnosis

    diagnosis = incident.get("selected_root_cause") or {}
    config = incident.get("config") or {}

    if not diagnosis.get("safe_to_auto_repair"):
        return {
            "ok": False,
            "reason": "selected repair is not safe_to_auto_repair",
        }

    result = repair_from_diagnosis(diagnosis, config=config)
    incident["repair_result"] = result
    return result



def store_incident_learning_sqlite(incident: dict) -> None:
    import json
    import sqlite3
    from datetime import datetime, UTC
    from pathlib import Path

    db_path = Path("data/reliability_reports.sqlite3")
    db_path.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(UTC).isoformat()
    selected = incident.get("selected_root_cause") or {}
    repair = incident.get("repair_result") or {}

    root_cause = selected.get("root_cause") or "unknown_failure"
    repair_action = selected.get("repair_action")
    confidence = float(selected.get("confidence") or 0.0)
    recovered = bool(incident.get("recovered"))

    conn = sqlite3.connect(db_path)

    try:
        conn.execute(
            """
            INSERT INTO reliability_reports (
                incident_id,
                created_at,
                root_cause,
                category,
                confidence,
                severity,
                summary,
                evidence_json,
                recommended_actions_json,
                repair_action,
                verification_level,
                is_repairable,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                incident.get("incident_id"),
                now,
                root_cause,
                selected.get("category"),
                confidence,
                "high" if selected.get("safe_to_auto_repair") else "medium",
                selected.get("summary") or incident.get("error"),
                json.dumps(incident.get("agent_findings") or {}, sort_keys=True, default=str),
                json.dumps([selected.get("repair_hint")], sort_keys=True),
                repair_action,
                "full",
                1 if selected.get("safe_to_auto_repair") else 0,
                json.dumps(
                    {
                        "runtime": incident.get("runtime"),
                        "strategy": incident.get("strategy"),
                        "operation": incident.get("operation"),
                        "recovered": recovered,
                        "repair_result": repair,
                    },
                    sort_keys=True,
                    default=str,
                ),
            ),
        )

        conn.execute(
            """
            INSERT OR REPLACE INTO root_cause_learnings (
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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                now,
                root_cause,
                str(incident.get("error") or "")[:500],
                1 if recovered else 0,
                0 if recovered else 1,
                repair_action,
                "full",
                selected.get("summary"),
                str(incident.get("error") or "")[:1000],
            ),
        )

        conn.execute(
            """
            INSERT INTO self_healing_runs (
                heal_id,
                created_at_utc,
                completed_at_utc,
                success,
                action,
                root_cause,
                stage,
                incident_id,
                project_dir,
                before_report_json,
                smoke_checks_json,
                commands_json,
                notes_json,
                error,
                runtime,
                strategy,
                details_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"heal_{incident.get('incident_id')}",
                now,
                now,
                1 if recovered else 0,
                repair_action or "none",
                root_cause,
                "agent_incident_commander",
                incident.get("incident_id"),
                "generated-pingora-proxy",
                json.dumps({"error": incident.get("error")}, sort_keys=True),
                json.dumps(incident.get("verification") or {}, sort_keys=True, default=str),
                json.dumps([], sort_keys=True),
                json.dumps(incident.get("repair_plan") or [], sort_keys=True, default=str),
                None if recovered else incident.get("error"),
                incident.get("runtime"),
                incident.get("strategy"),
                json.dumps(incident, sort_keys=True, default=str),
            ),
        )

        conn.commit()

    finally:
        conn.close()


def command_incident(
    *,
    runtime: str,
    strategy: str | None,
    operation: str,
    error: str,
    config: dict[str, Any] | None = None,
    allow_auto_repair: bool = True,
) -> dict[str, Any]:
    incident = make_incident(
        runtime=runtime,
        strategy=strategy,
        operation=operation,
        error=error,
        config=config,
    )

    run_agent_task(
        incident=incident,
        agent_name="root_cause_agent",
        task_fn=root_cause_task,
    )

    run_agent_task(
        incident=incident,
        agent_name="config_verification_agent",
        task_fn=config_verification_task,
    )

    run_agent_task(
        incident=incident,
        agent_name="runtime_probe_agent",
        task_fn=runtime_probe_task,
    )

    selected = select_root_cause(incident)
    build_repair_plan(incident)

    if allow_auto_repair and selected.get("safe_to_auto_repair"):
        execute_repair_plan(incident)

    try:
        store_incident_learning_sqlite(incident)
    except Exception as exc:
        incident.setdefault("warnings", []).append(
            {
                "stage": "sqlite_learning_store",
                "error": str(exc),
            }
        )

    save_incident_snapshot(incident)
    return incident


def record_runtime_failure_lesson(
    *,
    runtime: str,
    strategy: str | None,
    operation: str,
    error: str,
    config: dict | None = None,
    allow_auto_repair: bool = True,
) -> dict:
    """
    Automatic lesson recorder for real deploy/update/verify failures.

    Use this in catch blocks before raising the original failure.
    It classifies the failure, builds a repair plan, stores the incident
    into SQLite, and returns the incident object.
    """
    incident = command_incident(
        runtime=runtime,
        strategy=strategy,
        operation=operation,
        error=error,
        config=config or {},
        allow_auto_repair=allow_auto_repair,
    )

    return incident


def store_reliability_lesson(
    pattern_id: str,
    root_cause: str,
    category: str,
    summary: str,
    repair_hint: str,
    repair_action: str,
    safe_to_auto_repair: bool,
    markers: dict | None = None,
) -> dict:
    """
    Store or update a micro-lesson in reliability_knowledge.
    Handles older DB schemas by adding missing columns.
    """
    import json
    import sqlite3
    from datetime import datetime, timezone
    from pathlib import Path

    db = Path("data/reliability_reports.sqlite3")
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS reliability_knowledge (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pattern_id TEXT UNIQUE
    )
    """)

    required = {
        "root_cause": "TEXT",
        "category": "TEXT",
        "summary": "TEXT",
        "repair_hint": "TEXT",
        "repair_action": "TEXT",
        "safe_to_auto_repair": "INTEGER DEFAULT 0",
        "markers_json": "TEXT",
        "created_at_utc": "TEXT",
    }

    existing = {row[1] for row in conn.execute("PRAGMA table_info(reliability_knowledge)")}
    for col, typ in required.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE reliability_knowledge ADD COLUMN {col} {typ}")

    conn.execute(
        """
        INSERT INTO reliability_knowledge (
            pattern_id, root_cause, category, summary, repair_hint,
            repair_action, safe_to_auto_repair, markers_json, created_at_utc
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
            root_cause,
            category,
            summary,
            repair_hint,
            repair_action,
            1 if safe_to_auto_repair else 0,
            json.dumps(markers or {}, sort_keys=True),
            datetime.now(timezone.utc).isoformat(),
        ),
    )

    conn.commit()
    conn.close()

    return {
        "stored": True,
        "pattern_id": pattern_id,
        "root_cause": root_cause,
        "category": category,
        "repair_action": repair_action,
        "safe_to_auto_repair": safe_to_auto_repair,
    }


def store_or_update_lesson_lightweight(
    pattern_id: str,
    root_cause: str,
    category: str,
    summary: str,
    repair_action: str | None = None,
    safe_to_auto_repair: bool = False,
    repair_success: bool | None = None,
) -> dict:
    """
    Lightweight learning:
    - one row per pattern_id
    - increment seen_count
    - update success/failure counters
    - no duplicate growth
    """
    import sqlite3
    from datetime import datetime, timezone
    from pathlib import Path

    db = Path("data/reliability_reports.sqlite3")
    db.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS reliability_knowledge (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pattern_id TEXT UNIQUE
    )
    """)

    required = {
        "root_cause": "TEXT",
        "category": "TEXT",
        "summary": "TEXT",
        "repair_action": "TEXT",
        "safe_to_auto_repair": "INTEGER DEFAULT 0",
        "seen_count": "INTEGER DEFAULT 0",
        "repair_success_count": "INTEGER DEFAULT 0",
        "repair_failed_count": "INTEGER DEFAULT 0",
        "last_seen_utc": "TEXT",
        "archived": "INTEGER DEFAULT 0",
    }

    existing = {row[1] for row in conn.execute("PRAGMA table_info(reliability_knowledge)")}
    for col, typ in required.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE reliability_knowledge ADD COLUMN {col} {typ}")

    now = datetime.now(timezone.utc).isoformat()

    success_inc = 1 if repair_success is True else 0
    failed_inc = 1 if repair_success is False else 0

    conn.execute(
        """
        INSERT INTO reliability_knowledge (
            pattern_id,
            root_cause,
            category,
            summary,
            repair_action,
            safe_to_auto_repair,
            seen_count,
            repair_success_count,
            repair_failed_count,
            last_seen_utc,
            archived
        )
        VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 0)
        ON CONFLICT(pattern_id) DO UPDATE SET
            root_cause=excluded.root_cause,
            category=excluded.category,
            summary=excluded.summary,
            repair_action=excluded.repair_action,
            safe_to_auto_repair=excluded.safe_to_auto_repair,
            seen_count=COALESCE(reliability_knowledge.seen_count, 0) + 1,
            repair_success_count=COALESCE(reliability_knowledge.repair_success_count, 0) + ?,
            repair_failed_count=COALESCE(reliability_knowledge.repair_failed_count, 0) + ?,
            last_seen_utc=excluded.last_seen_utc
        """,
        (
            pattern_id,
            root_cause,
            category,
            summary,
            repair_action,
            1 if safe_to_auto_repair else 0,
            success_inc,
            failed_inc,
            now,
            success_inc,
            failed_inc,
        ),
    )

    conn.commit()

    row = conn.execute("""
        SELECT pattern_id, seen_count, repair_success_count, repair_failed_count, archived
        FROM reliability_knowledge
        WHERE pattern_id = ?
    """, (pattern_id,)).fetchone()

    conn.close()

    return {
        "stored": True,
        "pattern_id": pattern_id,
        "seen_count": row[1] if row else None,
        "repair_success_count": row[2] if row else None,
        "repair_failed_count": row[3] if row else None,
        "archived": row[4] if row else None,
    }
