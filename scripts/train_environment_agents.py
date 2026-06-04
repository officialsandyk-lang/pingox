from __future__ import annotations

import json
import sqlite3
from datetime import datetime, UTC
from pathlib import Path


DB_PATH = Path("data/reliability_reports.sqlite3")


ENVIRONMENT_LESSONS = [
    {
        "pattern_id": "docker_backend_port_unreachable",
        "category": "docker_backend_health",
        "root_cause": "docker_backend_port_unreachable",
        "summary": "Docker gateway returned 502 because the expected host backend was not reachable during verification.",
        "repair_hint": "Start required host backend ports before Docker deployment verification and confirm host.docker.internal reachability.",
        "markers": [
            "docker",
            "502",
            "/api live route check",
            "backend_evidence': False",
            "expected_ports",
            "CRUD GET"
        ],
        "repair_action": "start_missing_demo_backends_for_config",
        "safe_to_auto_repair": True
    },
    {
        "pattern_id": "docker_tls_api_key_expected",
        "category": "docker_edge_auth",
        "root_cause": "docker_tls_api_key_expected",
        "summary": "Docker public API route requires HTTPS and X-API-Key.",
        "repair_hint": "Use https://127.0.0.1:8443 with X-API-Key for protected routes; use /__edge_health for unauthenticated health.",
        "markers": [
            "401 Authorization Required",
            "https://127.0.0.1:8443/api",
            "X-API-Key",
            "__edge_health"
        ],
        "repair_action": "use_tls_api_key_or_edge_health",
        "safe_to_auto_repair": True
    },
    {
        "pattern_id": "local_docker_edge_active_mismatch",
        "category": "environment_selection",
        "root_cause": "local_docker_edge_active_mismatch",
        "summary": "Local guardian was run while Docker edge was active on port 8090.",
        "repair_hint": "Deploy local before local guardian, or run docker guardian for active Docker edge.",
        "markers": [
            "CERTIFICATE_VERIFY_FAILED",
            "http://127.0.0.1:8090",
            "runtime mismatch",
            "expected local",
            "got docker_host"
        ],
        "repair_action": "detect_active_environment_before_guardian",
        "safe_to_auto_repair": False
    },
    {
        "pattern_id": "bare_metal_local_config_mismatch",
        "category": "bare_metal_config",
        "root_cause": "bare_metal_local_config_mismatch",
        "summary": "Bare-metal guardian used local config that belonged to Docker/local instead of remote VM.",
        "repair_hint": "Skip local config integrity for bare-metal or fetch remote config over SSH.",
        "markers": [
            "expected bare_metal_linux",
            "got docker_host",
            "Bare-metal config lives on the remote host",
            "local generated config may belong to local/docker"
        ],
        "repair_action": "skip_or_fetch_remote_bare_metal_config",
        "safe_to_auto_repair": True
    },
    {
        "pattern_id": "bare_metal_ssh_cleanup_self_kill",
        "category": "bare_metal_ssh",
        "root_cause": "bare_metal_ssh_cleanup_self_kill",
        "summary": "Remote cleanup command killed its own SSH shell due to pkill -f matching the command text.",
        "repair_hint": "Use bracket-safe pkill patterns such as [p]ingora-gateway and [s]imple_backend.",
        "markers": [
            "ssh",
            "exit code 255",
            "pkill -f",
            "pingora-gateway",
            "simple_backend"
        ],
        "repair_action": "use_safe_pkill_patterns",
        "safe_to_auto_repair": True
    },
    {
        "pattern_id": "static_route_algorithm_false_warning",
        "category": "config_verification",
        "root_cause": "static_route_algorithm_false_warning",
        "summary": "Config verifier treated static / route or single backend proxy as unsupported algorithm.",
        "repair_hint": "Skip algorithm validation for static routes and allow single for one-upstream proxy routes.",
        "markers": [
            "unsupported algorithm single",
            "/: unsupported algorithm single",
            "/api: unsupported algorithm single"
        ],
        "repair_action": "allow_single_and_skip_static_algorithm_validation",
        "safe_to_auto_repair": True
    }
]


def main() -> int:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).isoformat()

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
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

    for item in ENVIRONMENT_LESSONS:
        conn.execute(
            """
            INSERT OR REPLACE INTO reliability_knowledge (
                pattern_id,
                created_at_utc,
                category,
                root_cause,
                summary,
                repair_hint,
                markers_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["pattern_id"],
                now,
                item["category"],
                item["root_cause"],
                item["summary"],
                item["repair_hint"],
                json.dumps(
                    {
                        "markers": item["markers"],
                        "repair_action": item["repair_action"],
                        "safe_to_auto_repair": item["safe_to_auto_repair"],
                    },
                    sort_keys=True,
                ),
            ),
        )
        print(f"trained: {item['pattern_id']}")

    conn.commit()
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
