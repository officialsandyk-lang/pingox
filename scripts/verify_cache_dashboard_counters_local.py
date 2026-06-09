from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROJECT_WRITER = ROOT / "core/project_writer.py"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    print("🧪 Verifying dashboard cache counter display")

    text = PROJECT_WRITER.read_text(encoding="utf-8")

    require("cacheCounterCard" in text, "cache counter card missing")
    require("Cache Counters" in text, "cache counter title missing")
    require("cacheEligibleCount" in text, "eligible counter id missing")
    require("cacheBypassCount" in text, "bypass counter id missing")
    require("updateCacheCountersFromRuntimeState" in text, "cache counter updater missing")
    require('/__gateway_runtime_state' in text, "runtime-state fetch missing")
    require("data.cache" in text, "runtime cache object not read")
    require("No cached response serving yet" in text, "safety note missing")

    for token in ["cache_store", "serve_cached", "insert_cache"]:
        require(token not in text, f"response cache behavior appeared too early: {token}")

    print("✅ Dashboard cache counter card present")
    print("✅ Dashboard reads runtime-state cache counters")
    print("✅ No response cache serving added")
    print("✅ CACHE DASHBOARD COUNTERS LOCAL VERIFIER PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
