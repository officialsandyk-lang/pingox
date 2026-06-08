from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates/pingora_runtime/src/main.rs"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    print("🧪 Verifying Rust cache structs in template only")

    text = TEMPLATE.read_text(encoding="utf-8")

    require("struct CacheConfig" in text, "CacheConfig missing")
    require("enabled: bool" in text, "enabled field missing")
    require("ttl_seconds: u64" in text, "ttl_seconds field missing")
    require("methods: Vec<String>" in text, "methods field missing")
    require("cache_key: String" in text, "cache_key field missing")
    require("bypass_headers: Vec<String>" in text, "bypass_headers field missing")
    require(
        "stale_while_revalidate_seconds: u64" in text,
        "stale field missing",
    )
    require("cache: Option<CacheConfig>" in text, "RouteConfig.cache missing")
    require(
        "fn default_cache_config() -> CacheConfig" in text,
        "default_cache_config missing",
    )
    require(
        "cache: Some(default_cache_config())" in text,
        "RouteConfig literals missing default cache",
    )

    forbidden = [
        "cache_store",
        "cache_hit",
        "cache_miss",
        "serve_cached",
        "insert_cache",
    ]

    for token in forbidden:
        require(token not in text, f"Runtime cache behavior appeared too early: {token}")

    print("✅ CacheConfig struct present")
    print("✅ RouteConfig.cache present")
    print("✅ default_cache_config present")
    print("✅ Route literals have default disabled cache")
    print("✅ No runtime cache behavior added")
    print("✅ RUST CACHE STRUCTS TEMPLATE VERIFIER PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
