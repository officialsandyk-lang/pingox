#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8080"
ROUTE = "/reports"
TTL = "30"


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    print("$ " + " ".join(cmd))
    result = subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    print(result.stdout)

    if check and result.returncode != 0:
        raise SystemExit(f"Command failed with exit {result.returncode}: {' '.join(cmd)}")

    return result


def get_json(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def request_headers(path: str) -> dict[str, str]:
    req = urllib.request.Request(BASE + path, method="GET")
    with urllib.request.urlopen(req, timeout=5) as response:
        return {k.lower(): v for k, v in response.headers.items()}


def cache_enabled_paths() -> set[str]:
    state = get_json("/__gateway_runtime_state")
    cache = state.get("cache", {})
    policy = cache.get("policy", {})
    routes = policy.get("enabled_routes", [])
    return {str(item.get("path")) for item in routes if isinstance(item, dict)}


def assert_runtime_healthy() -> None:
    state = get_json("/__gateway_runtime_state")
    if not state.get("ok"):
        raise SystemExit(f"Runtime state not ok: {state}")


def assert_cache_header(path: str, expected: str) -> None:
    headers = request_headers(path)
    value = headers.get("x-pingora-ai-cache", "")
    if value.upper() != expected:
        raise SystemExit(f"Expected cache {expected}, got {value!r} for {path}")
    print(f"✅ {path} cache header: {value}")


def main() -> None:
    print("🧪 Verifying local cache CLI control")

    run(["pingora-ai", "--help"])
    run(["pingora-ai", "cache", "status", "--local"])

    assert_runtime_healthy()

    print(f"🧹 Disable {ROUTE} first to make test deterministic")
    run(["pingora-ai", "cache", "disable", "--local", ROUTE])

    paths = cache_enabled_paths()
    if ROUTE in paths:
        raise SystemExit(f"{ROUTE} still enabled after disable: {paths}")
    print(f"✅ {ROUTE} disabled")

    print(f"✅ Enable cache for {ROUTE}")
    run(["pingora-ai", "cache", "enable", "--local", ROUTE, "--ttl", TTL])

    paths = cache_enabled_paths()
    if ROUTE not in paths:
        raise SystemExit(f"{ROUTE} missing from enabled cache routes: {paths}")
    print(f"✅ {ROUTE} enabled")

    unique = f"{ROUTE}/?cache_cli_verifier={int(time.time() * 1000)}"
    assert_cache_header(unique, "MISS")
    assert_cache_header(unique, "HIT")

    print("✅ Cache status after HIT")
    run(["pingora-ai", "cache", "status", "--local"])

    print("✅ Clear live cache")
    run(["pingora-ai", "cache", "clear", "--local"])

    state = get_json("/__gateway_runtime_state")
    entries = int(state.get("cache", {}).get("entries", -1))
    if entries != 0:
        raise SystemExit(f"Expected cache entries 0 after clear, got {entries}")
    print("✅ Cache entries cleared")

    print(f"✅ Disable cache for {ROUTE}")
    run(["pingora-ai", "cache", "disable", "--local", ROUTE])

    paths = cache_enabled_paths()
    if ROUTE in paths:
        raise SystemExit(f"{ROUTE} still enabled after final disable: {paths}")

    assert_runtime_healthy()
    print("✅ CACHE CLI CONTROL LOCAL VERIFICATION PASSED")


if __name__ == "__main__":
    main()
