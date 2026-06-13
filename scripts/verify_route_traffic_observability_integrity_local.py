#!/usr/bin/env python3
from __future__ import annotations

import json
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8080"

EXPECTED_ROUTES = {
    "/api",
    "/users",
    "/inventory",
    "/reports",
    "/search",
    "/billing",
}


def fail(message: str) -> None:
    raise SystemExit(f"❌ {message}")


def get_json(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def hit(path: str, count: int = 1) -> None:
    for _ in range(count):
        with urllib.request.urlopen(BASE + path, timeout=5) as response:
            if response.status != 200:
                fail(f"{path} returned HTTP {response.status}")


def normalize_upstreams(route: dict) -> list[str]:
    output: list[str] = []

    for item in route.get("upstreams", []) or []:
        if isinstance(item, dict):
            address = item.get("address")
        else:
            address = str(item)

        if address and address not in output:
            output.append(address)

    return output


def route_map_from_runtime(state: dict) -> dict[str, dict]:
    return {
        route.get("path"): route
        for route in state.get("routes", []) or []
        if isinstance(route, dict) and route.get("path")
    }


def traffic_map_from_runtime(state: dict) -> dict[str, dict]:
    traffic = state.get("route_traffic")

    if not isinstance(traffic, list):
        fail("runtime_state missing route_traffic list")

    return {
        item.get("path"): item
        for item in traffic
        if isinstance(item, dict) and item.get("path")
    }


def snapshot_runtime_contract() -> dict:
    state = get_json("/__gateway_runtime_state")

    if not state.get("ok"):
        fail("runtime_state ok=false")

    routes = route_map_from_runtime(state)
    traffic = traffic_map_from_runtime(state)

    missing_routes = sorted(EXPECTED_ROUTES - set(routes))
    if missing_routes:
        fail(f"missing expected routes: {missing_routes}")

    missing_traffic = sorted(EXPECTED_ROUTES - set(traffic))
    if missing_traffic:
        fail(f"missing route_traffic rows: {missing_traffic}")

    contract = {
        "routes": {
            path: {
                "balancing": routes[path].get("balancing"),
                "upstreams": normalize_upstreams(routes[path]),
            }
            for path in sorted(EXPECTED_ROUTES)
        },
        "cache_policy": state.get("cache", {}).get("policy", {}),
        "security": state.get("security", {}),
    }

    return {
        "state": state,
        "routes": routes,
        "traffic": traffic,
        "contract": contract,
    }


def traffic_total(item: dict) -> int:
    return int(item.get("total_requests", 0) or 0)


def main() -> None:
    print("🧪 Verifying route traffic observability integrity")

    before = snapshot_runtime_contract()
    before_contract = before["contract"]

    before_totals = {
        path: traffic_total(before["traffic"][path])
        for path in EXPECTED_ROUTES
    }

    traffic_paths = {
        "/api": "/api/?traffic_obs=1",
        "/users": "/users/?traffic_obs=1",
        "/inventory": "/inventory/?traffic_obs=1",
        "/reports": "/reports/?traffic_obs=1",
        "/search": "/search/?traffic_obs=1",
        "/billing": "/billing/?traffic_obs=1",
    }

    for path, request_path in traffic_paths.items():
        hit(request_path, count=3)

    after = snapshot_runtime_contract()
    after_contract = after["contract"]

    if after_contract != before_contract:
        print("Before contract:")
        print(json.dumps(before_contract, indent=2, sort_keys=True))
        print("After contract:")
        print(json.dumps(after_contract, indent=2, sort_keys=True))
        fail("route/cache/security contract changed during observability test")

    for path in EXPECTED_ROUTES:
        before_total = before_totals[path]
        after_total = traffic_total(after["traffic"][path])

        if after_total < before_total + 3:
            fail(
                f"{path} traffic counter did not increase enough: "
                f"before={before_total}, after={after_total}"
            )

        status_2xx = int(after["traffic"][path].get("status_2xx", 0) or 0)
        if status_2xx < 1:
            fail(f"{path} status_2xx did not increase or is missing")

    print("Final route traffic:")
    print(json.dumps(after["state"].get("route_traffic"), indent=2))

    print("✅ ROUTE TRAFFIC OBSERVABILITY INTEGRITY LOCAL VERIFICATION PASSED")


if __name__ == "__main__":
    main()
