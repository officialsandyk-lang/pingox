#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8080"


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    print("\n$ " + " ".join(cmd))
    result = subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    if result.stdout:
        print(result.stdout)

    if check and result.returncode != 0:
        raise SystemExit(f"❌ Command failed with exit {result.returncode}: {' '.join(cmd)}")

    return result


def get_text(path: str) -> str:
    with urllib.request.urlopen(BASE + path, timeout=10) as response:
        return response.read().decode("utf-8", errors="replace")


def get_json(path: str) -> dict:
    return json.loads(get_text(path))


def assert_runtime_healthy() -> None:
    state = get_json("/__gateway_runtime_state")

    if not state.get("ok"):
        raise SystemExit(f"❌ Runtime state is not ok: {state}")

    routes = state.get("routes", [])
    if not isinstance(routes, list) or not routes:
        raise SystemExit("❌ Runtime state has no routes")

    print("✅ Runtime state healthy")


def assert_dashboard_route_traffic_markers() -> None:
    html = get_text("/")

    required = [
        "Route Traffic",
        "routeTrafficTotal",
        "routeTrafficStats",
        "route-traffic-list",
        "route-traffic-row",
    ]

    missing = [marker for marker in required if marker not in html]

    if missing:
        raise SystemExit(f"❌ Dashboard missing route traffic markers: {missing}")

    print("✅ Dashboard route traffic markers present")


def assert_route_traffic_present() -> None:
    state = get_json("/__gateway_runtime_state")
    traffic = state.get("route_traffic")

    if not isinstance(traffic, list):
        raise SystemExit("❌ runtime_state missing route_traffic list")

    paths = {item.get("path") for item in traffic if isinstance(item, dict)}

    required = {"/api", "/users", "/inventory", "/reports", "/search", "/billing"}
    missing = sorted(required - paths)

    if missing:
        raise SystemExit(f"❌ route_traffic missing routes: {missing}")

    print("✅ Runtime route_traffic present for expected routes")


def assert_cache_policy_present() -> None:
    state = get_json("/__gateway_runtime_state")
    cache = state.get("cache", {})
    policy = cache.get("policy", {})

    if not isinstance(policy, dict):
        raise SystemExit("❌ Cache policy missing from runtime_state")

    if "enabled_routes" not in policy:
        raise SystemExit("❌ Cache policy missing enabled_routes")

    print("✅ Cache policy present")


def main() -> None:
    print("🧪 LOCAL PRODUCTION READINESS GATE")
    print("Scope: local-only, safe verifiers, no Docker/Linux mutation")

    assert_runtime_healthy()
    assert_dashboard_route_traffic_markers()
    assert_route_traffic_present()
    assert_cache_policy_present()

    run(["python", "scripts/verify_route_traffic_observability_integrity_local.py"])
    run(["python", "scripts/verify_dashboard_endpoint_integrity_local.py"])
    run(["python", "scripts/verify_cache_per_route_stats_local.py"])
    run(["python", "scripts/verify_cache_cli_control_local.py"])

    runtime_admin_script = ROOT / "scripts" / "verify_runtime_admin_local.py"
    if runtime_admin_script.exists():
        run(["python", "scripts/verify_runtime_admin_local.py"])
    else:
        print("⚠️ Skipping runtime admin verifier: scripts/verify_runtime_admin_local.py not found")

    dashboard_buttons_script = ROOT / "scripts" / "verify_dashboard_buttons_local.py"
    if dashboard_buttons_script.exists():
        run(["python", "scripts/verify_dashboard_buttons_local.py"])
    else:
        print("⚠️ Skipping dashboard buttons verifier: scripts/verify_dashboard_buttons_local.py not found")

    run([
        "pytest",
        "-q",
        "tests/test_add_single_backend_route_intent.py",
        "tests/test_single_upstream_algorithm_guard.py",
    ])

    assert_runtime_healthy()
    assert_dashboard_route_traffic_markers()
    assert_route_traffic_present()
    assert_cache_policy_present()

    print("\n✅ LOCAL PRODUCTION READINESS GATE PASSED")


if __name__ == "__main__":
    main()
