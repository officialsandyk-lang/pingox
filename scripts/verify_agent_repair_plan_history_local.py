#!/usr/bin/env python3
from __future__ import annotations

import json
import urllib.request


CONTROL = "http://127.0.0.1:8787"
DASHBOARD = "http://127.0.0.1:8080/"
TOKEN = "dev-local-control-token"


def fail(message: str) -> None:
    raise SystemExit(f"❌ {message}")


def get_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"X-Pingora-Control-Token": TOKEN},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def get_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.read().decode("utf-8", errors="replace")


def main() -> None:
    print("🧪 Verifying Agent repair plan history")

    dry_run = get_json(
        f"{CONTROL}/__gateway_control/agent/repair-plan/dry-run"
        "?root_cause=docker_backend_port_unreachable"
        "&repair_action=start_missing_demo_backends_for_config"
    )
    if not dry_run.get("ok"):
        fail(f"dry-run repair plan failed: {dry_run}")

    history = get_json(f"{CONTROL}/__gateway_control/agent/repair-plan/history?limit=5")
    if not history.get("ok"):
        fail(f"repair history endpoint failed: {history}")

    rows = history.get("history") or []
    if not rows:
        fail("repair history is empty after generating dry-run plan")

    required = [
        "created_at",
        "root_cause",
        "command_preview",
        "will_execute",
        "mode",
    ]
    missing = [key for key in required if key not in rows[0]]
    if missing:
        fail(f"repair history row missing fields: {missing}")

    if rows[0].get("will_execute") is not False:
        fail("history dry-run row must have will_execute=false")

    if rows[0].get("mode") != "dry_run_only":
        fail(f"history mode must be dry_run_only: {rows[0].get('mode')}")

    html = get_text(DASHBOARD)
    markers = [
        "viewRepairHistoryButton",
        "fetchAgentRepairHistory",
        "/__gateway_control/agent/repair-plan/history",
        "View Repair History",
    ]
    missing_markers = [marker for marker in markers if marker not in html]
    if missing_markers:
        fail(f"dashboard missing repair history markers: {missing_markers}")

    print("✅ Repair plan history endpoint stores dry-run plans")
    print("✅ Repair history dashboard markers present")
    print("✅ AGENT REPAIR PLAN HISTORY LOCAL VERIFICATION PASSED")


if __name__ == "__main__":
    main()
