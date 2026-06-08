from __future__ import annotations

from agents.config_repair_agent import repair_config


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def route_by_path(config: dict, path: str) -> dict:
    for route in config.get("routes", []) or []:
        if isinstance(route, dict) and route.get("path") == path:
            return route
    raise AssertionError(f"Missing route {path}")


def main() -> int:
    print("🧪 Verifying cache planner/config repair support")

    raw_config = {
        "routes": [
            {
                "path": "/api",
                "upstreams": [{"address": "127.0.0.1:9101"}],
            },
            {
                "path": "/cached",
                "upstreams": [{"address": "127.0.0.1:9102"}],
                "cache": {
                    "enabled": True,
                    "ttl_seconds": 999999,
                    "methods": ["POST", "DELETE"],
                    "cache_key": "bad-key",
                    "bypass_headers": [],
                    "stale_while_revalidate_seconds": 99999,
                    "unknown": "must-be-removed",
                },
            },
        ]
    }

    repaired = repair_config(raw_config, prompt="local cache repair verifier")

    api_cache = route_by_path(repaired, "/api").get("cache")
    cached_cache = route_by_path(repaired, "/cached").get("cache")

    require(isinstance(api_cache, dict), "/api cache policy missing")
    require(api_cache["enabled"] is False, "missing cache should repair to disabled")
    require(api_cache["methods"] == ["GET"], "default methods should be GET")
    require("authorization" in api_cache["bypass_headers"], "default authorization bypass missing")
    require("x-api-key" in api_cache["bypass_headers"], "default x-api-key bypass missing")

    require(isinstance(cached_cache, dict), "/cached cache policy missing")
    require(cached_cache["enabled"] is True, "enabled cache should remain enabled")
    require(cached_cache["ttl_seconds"] == 86400, "ttl should clamp to 86400")
    require(cached_cache["methods"] == ["GET"], "unsafe methods should normalize to GET")
    require(cached_cache["cache_key"] == "method:path:query", "bad cache_key should normalize")
    require(cached_cache["stale_while_revalidate_seconds"] == 3600, "stale should clamp to 3600")
    require("authorization" in cached_cache["bypass_headers"], "authorization bypass should be forced")
    require("x-api-key" in cached_cache["bypass_headers"], "x-api-key bypass should be forced")
    require("unknown" not in cached_cache, "unknown cache keys should not survive repair")

    print("✅ Missing cache policies repaired to disabled safe default")
    print("✅ Unsafe cache methods normalized")
    print("✅ TTL and stale values clamped")
    print("✅ Cache key normalized")
    print("✅ Auth/API-key bypass forced")
    print("✅ Unknown cache keys removed")
    print("✅ CACHE PLANNER/CONFIG REPAIR VERIFIER PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
