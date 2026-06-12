from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = "http://127.0.0.1:8080"


def fail(message: str) -> None:
    raise SystemExit(f"❌ {message}")


def run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, cwd=ROOT, text=True)
    if result.returncode != 0:
        fail(f"Command failed: {' '.join(cmd)}")


def get_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=10) as res:
        return res.read().decode("utf-8", errors="replace")


def get_json(url: str) -> dict[str, Any]:
    return json.loads(get_text(url))


def wait_ready() -> dict[str, Any]:
    last = ""
    for _ in range(30):
        try:
            data = get_json(f"{RUNTIME}/__gateway_runtime_state")
            if data.get("ok") is True:
                return data
            last = json.dumps(data)
        except Exception as exc:
            last = str(exc)
        time.sleep(1)
    fail(f"Runtime not ready. Last: {last}")


def main() -> int:
    print("🧪 Verifying cache policy observability local")

    run(["python", "-m", "pingora_ai.cli", "start", "--local"])
    state = wait_ready()

    cache = state.get("cache") or {}
    policy = cache.get("policy") or {}
    enabled_routes = policy.get("enabled_routes") or []

    print("cache policy:", json.dumps(policy, indent=2))

    if policy.get("max_entries") != 1024:
        fail("cache.policy.max_entries should be 1024")

    if policy.get("max_body_bytes") != 1048576:
        fail("cache.policy.max_body_bytes should be 1048576")

    if not enabled_routes:
        fail("cache.policy.enabled_routes is empty")

    api_policy = None
    for item in enabled_routes:
        if item.get("path") == "/api":
            api_policy = item
            break

    if not api_policy:
        fail("cache.policy.enabled_routes missing /api")

    if int(api_policy.get("ttl_seconds") or 0) < 1:
        fail("/api cache ttl_seconds missing or invalid")

    methods = api_policy.get("methods") or []
    if "GET" not in methods:
        fail("/api cache methods missing GET")

    if "HEAD" not in methods:
        fail("/api cache methods missing HEAD")

    cache_key = api_policy.get("cache_key")
    if cache_key not in {"method:path:query", "method:path"}:
        fail(f"/api cache_key invalid: {cache_key}")

    bypass_headers = [str(item).lower() for item in (api_policy.get("bypass_headers") or [])]

    if "authorization" not in bypass_headers:
        fail("/api bypass_headers missing authorization")

    if "x-api-key" not in bypass_headers:
        fail("/api bypass_headers missing x-api-key")

    html = get_text(f"{RUNTIME}/")
    required_markers = [
        "Cache Policy",
        "cachePolicyMaxEntries",
        "cachePolicyMaxBodyBytes",
        "cachePolicyRoutes",
        "cachePolicyTtl",
        "cachePolicyMethods",
        "cachePolicyBypassHeaders",
        "Max entries",
        "Max body bytes",
        "Bypass headers",
    ]

    missing = [marker for marker in required_markers if marker not in html]
    if missing:
        fail(f"dashboard missing cache policy markers: {missing}")

    print("✅ Runtime state cache policy correct")
    print("✅ Dashboard cache policy markers correct")
    print("✅ CACHE POLICY OBSERVABILITY LOCAL VERIFICATION PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
