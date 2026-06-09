from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from pathlib import Path

from core.project_writer import rewrite_rust_build_routes, rewrite_rust_listen_port


ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "generated-pingora-proxy"
CONFIG = PROJECT / "config.local.json"
if not CONFIG.exists():
    CONFIG = PROJECT / "config.json"

BASE = "http://127.0.0.1:8080"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def get_json(url: str, headers: dict[str, str] | None = None) -> dict:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def get_text(url: str, headers: dict[str, str] | None = None) -> str:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=5) as response:
        return response.read().decode("utf-8", errors="replace")


def ensure_test_cache_route() -> None:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))

    routes = config.get("routes")
    require(isinstance(routes, list), "config routes must be a list")

    target = None
    for route in routes:
        if isinstance(route, dict) and route.get("path") == "/api":
            target = route
            break

    require(target is not None, "existing /api route not found")

    target["cache"] = {
        "enabled": True,
        "ttl_seconds": 30,
        "methods": ["GET", "HEAD"],
        "cache_key": "method:path:query",
        "bypass_headers": ["authorization", "x-api-key"],
        "stale_while_revalidate_seconds": 0,
    }

    config["routes"] = routes
    CONFIG.write_text(json.dumps(config, indent=2), encoding="utf-8")

    # Important: generated Pingora routes are compiled into Rust source.
    # After enabling cache on /api in config, regenerate build_routes before restart.
    rewrite_rust_build_routes(config, PROJECT)
    rewrite_rust_listen_port(config, PROJECT)


def restart_local_runtime() -> None:
    # Always restart here so regenerated /api cache config is loaded.
    result = subprocess.run(
        ["python", "-m", "pingora_ai.cli", "start", "--local"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60,
    )
    print(result.stdout)

    started_signal = "Local Pingora runtime start requested" in result.stdout
    require(
        result.returncode == 0 or started_signal,
        "local runtime restart failed before readiness wait",
    )

    for _ in range(60):
        try:
            state = get_json(f"{BASE}/__gateway_runtime_state")
            if state.get("ok") is True:
                return
        except Exception:
            pass
        time.sleep(0.5)

    raise AssertionError("local runtime did not become ready")


def cache_stats() -> tuple[int, int]:
    state = get_json(f"{BASE}/__gateway_runtime_state")
    cache = state.get("cache")
    require(isinstance(cache, dict), "runtime_state missing cache object")
    return int(cache.get("eligible", 0)), int(cache.get("bypasses", 0))


def main() -> int:
    print("🧪 Verifying live cache eligibility/bypass counters")

    ensure_test_cache_route()
    restart_local_runtime()

    before_eligible, before_bypass = cache_stats()

    # GET without auth/api-key should be eligible.
    get_text(f"{BASE}/api")
    after_eligible, after_bypass = cache_stats()
    require(
        after_eligible > before_eligible,
        f"eligible counter did not increase: before={before_eligible} after={after_eligible}",
    )
    require(
        after_bypass == before_bypass,
        f"bypass counter unexpectedly changed: before={before_bypass} after={after_bypass}",
    )

    # GET with Authorization should bypass.
    get_text(f"{BASE}/api", headers={"Authorization": "Bearer test"})
    after_auth_eligible, after_auth_bypass = cache_stats()
    require(
        after_auth_bypass > after_bypass,
        f"bypass counter did not increase for Authorization: before={after_bypass} after={after_auth_bypass}",
    )
    require(
        after_auth_eligible == after_eligible,
        f"eligible counter changed during bypass: before={after_eligible} after={after_auth_eligible}",
    )

    # GET with X-API-Key should bypass.
    get_text(f"{BASE}/api", headers={"X-API-Key": "test"})
    final_eligible, final_bypass = cache_stats()
    require(
        final_bypass > after_auth_bypass,
        f"bypass counter did not increase for X-API-Key: before={after_auth_bypass} after={final_bypass}",
    )
    require(
        final_eligible == after_auth_eligible,
        f"eligible counter changed during x-api-key bypass: before={after_auth_eligible} after={final_eligible}",
    )

    print(f"✅ eligible counter increased: {before_eligible} -> {after_eligible}")
    print(f"✅ bypass counter increased: {before_bypass} -> {final_bypass}")
    print("✅ Authorization bypass proven")
    print("✅ X-API-Key bypass proven")
    print("✅ CACHE RUNTIME COUNTERS LOCAL VERIFIER PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
