from __future__ import annotations

import json
from pathlib import Path

from core.cache_policy import (
    normalize_cache_policy,
    normalize_config_cache_policies,
    validate_cache_policy,
    validate_config_cache_policies,
)


CONFIG_PATH = Path("generated-pingora-proxy/config.local.json")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    print("🧪 Verifying local cache config contract")

    require(CONFIG_PATH.exists(), f"Missing {CONFIG_PATH}")

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    normalized_config = normalize_config_cache_policies(config)

    ok, failures = validate_config_cache_policies(normalized_config)
    require(ok, f"Existing config cache validation failed: {failures}")

    routes = normalized_config.get("routes") or []
    require(isinstance(routes, list), "routes must be list")

    for route in routes:
        if not isinstance(route, dict):
            continue

        cache = route.get("cache")
        require(isinstance(cache, dict), f"{route.get('path')} cache must be object")
        require("enabled" in cache, f"{route.get('path')} cache.enabled missing")
        require("ttl_seconds" in cache, f"{route.get('path')} cache.ttl_seconds missing")
        require("methods" in cache, f"{route.get('path')} cache.methods missing")
        require("cache_key" in cache, f"{route.get('path')} cache.cache_key missing")
        require("bypass_headers" in cache, f"{route.get('path')} cache.bypass_headers missing")
        require(
            "authorization" in cache["bypass_headers"],
            f"{route.get('path')} must bypass authorization",
        )
        require(
            "x-api-key" in cache["bypass_headers"],
            f"{route.get('path')} must bypass x-api-key",
        )

    valid_policy = {
        "enabled": True,
        "ttl_seconds": 30,
        "methods": ["GET"],
        "cache_key": "method:path:query",
        "bypass_headers": ["authorization", "x-api-key"],
        "stale_while_revalidate_seconds": 10,
    }

    ok, errors, normalized = validate_cache_policy(valid_policy)
    require(ok, f"Valid cache policy failed: {errors}")
    require(normalized["enabled"] is True, "valid policy enabled mismatch")
    require(normalized["ttl_seconds"] == 30, "valid policy ttl mismatch")
    require(normalized["methods"] == ["GET"], "valid policy methods mismatch")

    unsafe_policy = {
        "enabled": True,
        "ttl_seconds": -10,
        "methods": ["POST", "DELETE"],
        "cache_key": "bad-key",
        "bypass_headers": [],
        "extra": "bad",
    }

    ok, errors, normalized = validate_cache_policy(unsafe_policy)
    require(ok is False, "Unsafe policy should fail because of unsupported key")
    require("authorization" in normalized["bypass_headers"], "unsafe normalized must add authorization bypass")
    require("x-api-key" in normalized["bypass_headers"], "unsafe normalized must add x-api-key bypass")
    require(normalized["methods"] == ["GET"], "unsafe methods should normalize to GET")
    require(normalized["ttl_seconds"] == 1, "unsafe ttl should clamp to 1")
    require(normalized["cache_key"] == "method:path:query", "unsafe cache_key should normalize")

    none_policy = normalize_cache_policy(None)
    require(none_policy["enabled"] is False, "missing cache should default disabled")
    require(none_policy["methods"] == ["GET"], "missing cache should default GET")

    print("✅ Existing local config cache policies valid")
    print("✅ Valid cache policy accepted")
    print("✅ Unsafe cache policy normalized safely")
    print("✅ Missing cache policy defaults to disabled")
    print("✅ CACHE CONFIG CONTRACT VERIFIER PASSED")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
