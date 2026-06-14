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
    print("🧪 Verifying Agent dry-run repair plans")

    data = get_json(
        f"{CONTROL}/__gateway_control/agent/repair-plan/dry-run"
        "?root_cause=docker_backend_port_unreachable"
        "&repair_action=start_missing_demo_backends_for_config"
    )

    if not data.get("ok"):
        fail(f"repair plan endpoint failed: {data}")

    plan = data.get("plan") or {}
    required = [
        "root_cause",
        "actionability",
        "repair_steps",
        "risk",
        "requires_confirmation",
        "command_preview",
        "verification_command",
        "will_execute",
    ]

    missing = [key for key in required if key not in plan]
    if missing:
        fail(f"repair plan missing fields: {missing}")

    if plan.get("will_execute") is not False:
        fail("dry-run repair plan must not execute")

    safety = data.get("safety") or {}
    unsafe = [key for key, value in safety.items() if value is not False]
    if unsafe:
        fail(f"dry-run safety flags must all be false: {unsafe}")

    html = get_text(DASHBOARD)
    markers = [
        "agentRepairPlanButton",
        "fetchAgentRepairPlan",
        "/__gateway_control/agent/repair-plan/dry-run",
        "Generate Repair Plan",
    ]
    missing_markers = [marker for marker in markers if marker not in html]
    if missing_markers:
        fail(f"dashboard missing repair plan markers: {missing_markers}")

    print("✅ Dry-run repair plan endpoint works")
    print("✅ Dashboard repair plan markers present")
    print("✅ AGENT DRY-RUN REPAIR PLAN LOCAL VERIFICATION PASSED")


if __name__ == "__main__":
    main()
