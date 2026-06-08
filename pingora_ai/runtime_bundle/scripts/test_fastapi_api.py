from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any


API_URL = os.environ.get("PINGORA_AI_API_URL", "http://127.0.0.1:8000").rstrip("/")
API_TOKEN = os.environ.get("PINGORA_AI_API_TOKEN", "dev-token-change-this")


def get_json(path: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{API_URL}{path}", timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def post_json(path: str, payload: dict[str, Any], token: str | None = None) -> dict[str, Any]:
    headers = {
        "Content-Type": "application/json",
    }

    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(
        url=f"{API_URL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def expect_http_error(path: str, payload: dict[str, Any], token: str, expected_code: int) -> None:
    try:
        post_json(path, payload, token=token)
    except urllib.error.HTTPError as exc:
        assert exc.code == expected_code, f"Expected HTTP {expected_code}, got {exc.code}"
        return

    raise AssertionError(f"Expected HTTP {expected_code}, but request succeeded")


def test_health() -> None:
    data = get_json("/health")

    assert data["ok"] is True
    assert data["service"] == "pingora-ai-api"

    print("✅ /health passed")


def test_invalid_token() -> None:
    payload = {
        "prompt": "create gateway with /api random across 127.0.0.1:9101 and 127.0.0.1:9102",
        "runtime": "docker_host",
        "strategy": "bluegreen",
        "verify": "full",
    }

    expect_http_error("/v1/plan", payload, token="wrong-token", expected_code=403)

    print("✅ invalid token protection passed")


def test_basic_plan() -> None:
    payload = {
        "prompt": (
            "create gateway with /api random across "
            "127.0.0.1:9101, 127.0.0.1:9102 and 127.0.0.1:9103"
        ),
        "runtime": "docker_host",
        "strategy": "bluegreen",
        "verify": "full",
    }

    data = post_json("/v1/plan", payload, token=API_TOKEN)

    assert data["ok"] is True

    config = data["config"]
    routes = config["routes"]

    api_route = next((route for route in routes if route["path"] == "/api"), None)

    assert api_route is not None, "Missing /api route"
    assert api_route["balancing"] == "random", api_route
    assert len(api_route["upstreams"]) == 3, api_route

    print("✅ /v1/plan basic random route passed")


def test_full_baseline_plan() -> None:
    payload = {
        "prompt": (
            "create gateway with /api random across 127.0.0.1:9101, "
            "127.0.0.1:9102 and 127.0.0.1:9103; "
            "/users round robin across 127.0.0.1:9101, 127.0.0.1:9102 "
            "and 127.0.0.1:9103; "
            "/orders weighted round robin across 127.0.0.1:9201 weight 5, "
            "127.0.0.1:9202 weight 2, 127.0.0.1:9203 weight 1; "
            "/payments ip hash across 127.0.0.1:9301 and 127.0.0.1:9302; "
            "/reports least connections across 127.0.0.1:9501, "
            "127.0.0.1:9502 and 127.0.0.1:9503; "
            "/inventory to backend 127.0.0.1:9401"
        ),
        "runtime": "docker_host",
        "strategy": "bluegreen",
        "verify": "full",
    }

    data = post_json("/v1/plan", payload, token=API_TOKEN)

    assert data["ok"] is True

    routes = {
        route["path"]: route
        for route in data["config"]["routes"]
    }

    expected_algorithms = {
        "/api": "random",
        "/users": "round_robin",
        "/orders": "weighted_round_robin",
        "/payments": "ip_hash",
        "/reports": "least_connections",
        "/inventory": "single",
    }

    for path, expected_algorithm in expected_algorithms.items():
        assert path in routes, f"Missing route: {path}"
        actual_algorithm = routes[path].get("balancing")
        assert actual_algorithm == expected_algorithm, (
            f"{path}: expected {expected_algorithm}, got {actual_algorithm}"
        )

    print("✅ full baseline route algorithm contract passed")


def main() -> int:
    print(f"Testing FastAPI API at: {API_URL}")

    test_health()
    test_invalid_token()
    test_basic_plan()
    test_full_baseline_plan()

    print()
    print("✅ All FastAPI API tests passed")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print()
        print(f"❌ FastAPI API test failed: {exc}")
        raise SystemExit(1)