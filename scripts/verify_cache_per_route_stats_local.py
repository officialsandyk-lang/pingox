from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8080"
ROUTE = "/api"


def fail(message: str) -> None:
    raise SystemExit(f"❌ {message}")


def request(path: str, method: str = "GET", headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], str]:
    req = urllib.request.Request(
        f"{BASE}{path}",
        method=method,
        headers=headers or {},
    )

    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            return (
                response.status,
                {key.lower(): value for key, value in response.headers.items()},
                response.read().decode("utf-8", errors="replace"),
            )
    except urllib.error.HTTPError as exc:
        return (
            exc.code,
            {key.lower(): value for key, value in exc.headers.items()},
            exc.read().decode("utf-8", errors="replace"),
        )


def json_get(path: str) -> dict:
    status, _headers, body = request(path)
    if status != 200:
        fail(f"GET {path} returned {status}: {body[:300]}")
    try:
        return json.loads(body)
    except Exception as exc:
        fail(f"GET {path} did not return JSON: {exc}: {body[:300]}")


def start_local_runtime() -> None:
    result = subprocess.run(
        [
            "python",
            "-m",
            "pingora_ai.cli",
            "start",
            "--local",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    if result.returncode != 0:
        print("⚠️ start --local returned non-zero; checking runtime readiness before failing")
        print((result.stdout or "")[-1200:])
        print((result.stderr or "")[-1200:])

    for _ in range(60):
        try:
            state = json_get("/__gateway_runtime_state")
            if state.get("ok") is True:
                return
        except Exception:
            pass
        time.sleep(0.25)

    if result.returncode != 0:
        fail(f"local runtime did not become ready after non-zero start --local exit: {result.returncode}")

    fail("local runtime did not become ready")


def clear_cache() -> None:
    status, _headers, body = request("/__gateway_runtime_admin/cache/clear", method="POST")
    if status != 200:
        fail(f"cache clear failed with {status}: {body[:300]}")

    data = json.loads(body)
    if data.get("ok") is not True:
        fail(f"cache clear returned non-ok body: {data}")


def cache_state() -> dict:
    state = json_get("/__gateway_runtime_state")
    cache = state.get("cache")
    if not isinstance(cache, dict):
        fail(f"runtime_state.cache missing or invalid: {state}")
    return cache


def route_stats(cache: dict, path: str) -> dict:
    stats = cache.get("per_route_stats")
    if not isinstance(stats, list):
        fail(f"cache.per_route_stats missing or not list: {cache}")

    for item in stats:
        if item.get("path") == path:
            return item

    fail(f"cache.per_route_stats missing route {path}: {stats}")


def main() -> None:
    print("🧪 Verifying local per-route cache stats")
    start_local_runtime()
    clear_cache()

    initial = cache_state()
    if "approximate_bytes" not in initial:
        fail(f"cache.approximate_bytes missing: {initial}")

    initial_route = route_stats(initial, ROUTE)
    print("Initial /api stats:", json.dumps(initial_route, indent=2))

    probe = f"{ROUTE}/?per_route_stats_probe={int(time.time() * 1000)}"

    first_status, first_headers, _first_body = request(probe)
    if first_status != 200:
        fail(f"first cache probe returned {first_status}")

    second_status, second_headers, _second_body = request(probe)
    if second_status != 200:
        fail(f"second cache probe returned {second_status}")

    if first_headers.get("x-pingora-ai-cache") != "MISS":
        fail(f"first response should be MISS, got {first_headers.get('x-pingora-ai-cache')}")

    if second_headers.get("x-pingora-ai-cache") != "HIT":
        fail(f"second response should be HIT, got {second_headers.get('x-pingora-ai-cache')}")

    status, _headers, _body = request(
        f"{ROUTE}/?per_route_stats_bypass={int(time.time() * 1000)}",
        headers={"authorization": "Bearer verifier"},
    )
    if status != 200:
        fail(f"bypass probe returned {status}")

    final = cache_state()
    final_route = route_stats(final, ROUTE)

    print("Final cache:", json.dumps(final, indent=2))
    print("Final /api stats:", json.dumps(final_route, indent=2))

    if int(final.get("entries", 0)) < 1:
        fail(f"cache.entries should be >= 1, got {final}")

    if int(final.get("approximate_bytes", 0)) < 1:
        fail(f"cache.approximate_bytes should be >= 1, got {final}")

    if int(final_route.get("entries", 0)) < 1:
        fail(f"/api entries should be >= 1, got {final_route}")

    if int(final_route.get("approximate_bytes", 0)) < 1:
        fail(f"/api approximate_bytes should be >= 1, got {final_route}")

    if int(final_route.get("misses", 0)) < 1:
        fail(f"/api misses should be >= 1, got {final_route}")

    if int(final_route.get("hits", 0)) < 1:
        fail(f"/api hits should be >= 1, got {final_route}")

    if int(final_route.get("bypasses", 0)) < 1:
        fail(f"/api bypasses should be >= 1, got {final_route}")

    if int(final_route.get("eligible", 0)) < 2:
        fail(f"/api eligible should be >= 2, got {final_route}")

    print("✅ CACHE PER-ROUTE STATS LOCAL VERIFICATION PASSED")


if __name__ == "__main__":
    main()
