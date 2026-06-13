#!/usr/bin/env python3
from __future__ import annotations

import json
import urllib.request


BASE = "http://127.0.0.1:8080"


def fail(message: str) -> None:
    raise SystemExit(f"❌ {message}")


def get_json(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> None:
    print("🧪 Verifying local active health policy")

    state = get_json("/__gateway_runtime_state")

    if not state.get("ok"):
        fail("runtime_state ok=false")

    active_health = state.get("active_health")
    if not isinstance(active_health, dict):
        fail("active_health missing")

    policy = active_health.get("policy")
    if not isinstance(policy, dict):
        fail("active_health.policy missing")

    required_policy_keys = {
        "path",
        "expected_status",
        "timeout_seconds",
        "interval_seconds",
        "healthy_threshold",
        "unhealthy_threshold",
        "body_contains",
        "header_name",
        "header_value",
    }

    missing_policy = sorted(required_policy_keys - set(policy))
    if missing_policy:
        fail(f"active_health.policy missing keys: {missing_policy}")

    if int(policy.get("expected_status", 0)) != 200:
        fail("expected_status should be 200")

    if int(policy.get("timeout_seconds", 0)) < 1:
        fail("timeout_seconds should be >= 1")

    if int(policy.get("healthy_threshold", 0)) < 1:
        fail("healthy_threshold should be >= 1")

    if int(policy.get("unhealthy_threshold", 0)) < 1:
        fail("unhealthy_threshold should be >= 1")

    upstreams = active_health.get("upstreams")
    if not isinstance(upstreams, list) or not upstreams:
        fail("active_health.upstreams missing or empty")

    required_upstream_keys = {
        "route",
        "upstream",
        "healthy",
        "last_status",
        "consecutive_successes",
        "consecutive_failures",
        "last_error",
        "last_checked_unix_ms",
    }

    for item in upstreams:
        if not isinstance(item, dict):
            fail("active_health upstream item is not object")

        missing = sorted(required_upstream_keys - set(item))
        if missing:
            fail(f"active_health upstream missing keys: {missing}")

        if not item.get("route"):
            fail("active_health upstream missing route")

        if not item.get("upstream"):
            fail("active_health upstream missing upstream")

    routes = state.get("routes", [])
    route_upstream_count = sum(len(route.get("upstreams", [])) for route in routes if isinstance(route, dict))

    if len(upstreams) < route_upstream_count:
        fail(f"active_health upstream rows too low: {len(upstreams)} < {route_upstream_count}")

    print("Active health policy:")
    print(json.dumps(policy, indent=2))

    print(f"✅ Active health upstream rows: {len(upstreams)}")
    print("✅ ACTIVE HEALTH POLICY LOCAL VERIFICATION PASSED")


if __name__ == "__main__":
    main()
