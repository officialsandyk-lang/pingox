#!/usr/bin/env python3
from __future__ import annotations

import json
import urllib.request


BASE = "http://127.0.0.1:8080"


def fail(message: str) -> None:
    raise SystemExit(f"❌ {message}")


def get_text(path: str) -> str:
    with urllib.request.urlopen(BASE + path, timeout=10) as response:
        return response.read().decode("utf-8", errors="replace")


def get_json(path: str) -> dict:
    return json.loads(get_text(path))


def main() -> None:
    print("🧪 Verifying active health dashboard")

    html = get_text("/")

    markers = [
        "Active Health",
        "activeHealthPolicy",
        "activeHealthHealthy",
        "activeHealthStats",
        "active-health-list",
        "active-health-row",
    ]

    missing = [marker for marker in markers if marker not in html]
    if missing:
        fail(f"dashboard missing markers: {missing}")

    state = get_json("/__gateway_runtime_state")
    active_health = state.get("active_health", {})
    policy = active_health.get("policy", {})
    upstreams = active_health.get("upstreams", [])

    if not isinstance(policy, dict) or not policy:
        fail("active_health.policy missing from runtime_state")

    if not isinstance(upstreams, list) or not upstreams:
        fail("active_health.upstreams missing from runtime_state")

    required_policy_keys = [
        "path",
        "expected_status",
        "timeout_seconds",
        "interval_seconds",
        "healthy_threshold",
        "unhealthy_threshold",
    ]

    for key in required_policy_keys:
        if key not in policy:
            fail(f"active_health.policy missing {key}")

    print("✅ Active Health dashboard markers present")
    print(f"✅ Active Health policy visible: {policy.get('path')} expected {policy.get('expected_status')}")
    print(f"✅ Active Health upstream rows available: {len(upstreams)}")
    print("✅ ACTIVE HEALTH DASHBOARD LOCAL VERIFICATION PASSED")


if __name__ == "__main__":
    main()
