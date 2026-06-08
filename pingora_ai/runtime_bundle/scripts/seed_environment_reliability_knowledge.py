#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, UTC
from pathlib import Path


DB_PATH = Path("data/reliability_reports.sqlite3")


PATTERNS = [
    {
        "pattern_id": "local_verifier_url_mismatch",
        "category": "environment_verification",
        "root_cause": "local_verifier_url_mismatch",
        "summary": "Local/direct deployment was verified using Docker/TLS edge URL.",
        "repair_hint": "Use http://127.0.0.1:8090 for local/direct post-change verification.",
        "markers": [
            "Live URL: https://127.0.0.1:8443",
            "homepage status None",
            "local direct verification failed",
            "https://127.0.0.1:8443",
        ],
        "repair_action": "set_local_live_url_to_8090",
    },
    {
        "pattern_id": "local_edge_health_endpoint_mismatch",
        "category": "environment_readiness",
        "root_cause": "local_edge_health_endpoint_mismatch",
        "summary": "Local runtime was checked using /__edge_health, but local Pingora does not expose it.",
        "repair_hint": "Use / or /api/ as local readiness endpoint.",
        "markers": [
            "__edge_health",
            "HTTP Error 404",
            "Timed out waiting",
        ],
        "repair_action": "set_local_readiness_to_homepage",
    },
    {
        "pattern_id": "bare_metal_static_homepage_missing",
        "category": "bare_metal_release_packaging",
        "root_cause": "bare_metal_static_homepage_missing",
        "summary": "Static homepage was generated locally but not uploaded to the active bare-metal release.",
        "repair_hint": "Upload generated-pingora-proxy/public/ to /opt/pingora-ai/releases/<color>/public/.",
        "markers": [
            "public_nginx_homepage",
            "404",
            "public/index.html",
            "homepage",
        ],
        "repair_action": "upload_static_assets_to_release",
    },
    {
        "pattern_id": "bare_metal_docker_host_address_leak",
        "category": "runtime_addressing",
        "root_cause": "bare_metal_docker_host_address_leak",
        "summary": "Docker-only host.docker.internal leaked into native Linux runtime.",
        "repair_hint": "Rewrite host.docker.internal to 127.0.0.1 or a private Linux address.",
        "markers": [
            "host.docker.internal",
            "resolve_failed",
            "failed to lookup address information",
        ],
        "repair_action": "rewrite_docker_host_to_loopback",
    },
    {
        "pattern_id": "bare_metal_systemd_stop_timeout",
        "category": "bare_metal_systemd",
        "root_cause": "bare_metal_systemd_stop_timeout",
        "summary": "Pingora process did not stop cleanly through systemd.",
        "repair_hint": "SIGKILL pingora-gateway first, then reset-failed. Avoid disable --now.",
        "markers": [
            "systemctl",
            "timed out",
            "stop-sigterm",
            "Failed with result 'timeout'",
        ],
        "repair_action": "kill_process_then_reset_failed",
    },
]


def main() -> int:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).isoformat()

    conn = sqlite3.connect(DB_PATH)

    for item in PATTERNS:
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
                        "safe_to_auto_repair": True,
                    },
                    sort_keys=True,
                ),
            ),
        )

        conn.execute(
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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                now,
                item["root_cause"],
                " | ".join(item["markers"]),
                1,
                0,
                item["repair_action"],
                "full",
                item["summary"],
                item["repair_hint"],
            ),
        )

        print(f"seeded: {item['pattern_id']}")

    conn.commit()
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
