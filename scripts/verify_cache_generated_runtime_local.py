from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / "generated-pingora-proxy/src/main.rs"
CARGO_TOML = ROOT / "generated-pingora-proxy/Cargo.toml"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    print("🧪 Verifying generated runtime cache structs and cargo check")

    text = GENERATED.read_text(encoding="utf-8")

    require("struct CacheConfig" in text, "CacheConfig missing in generated runtime")
    require("cache: Option<CacheConfig>" in text, "RouteConfig.cache missing in generated runtime")
    require("fn default_cache_config() -> CacheConfig" in text, "default_cache_config missing")
    require(
        "cache: Some(default_cache_config())" in text,
        "generated RouteConfig literals missing default cache",
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

    result = subprocess.run(
        ["cargo", "check", "--manifest-path", str(CARGO_TOML)],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    print(result.stdout)

    require(result.returncode == 0, "cargo check failed")

    print("✅ Generated runtime has cache structs")
    print("✅ Generated runtime has no live cache behavior")
    print("✅ cargo check passed")
    print("✅ CACHE GENERATED RUNTIME LOCAL VERIFIER PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
