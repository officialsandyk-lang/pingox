from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates/pingora_runtime/src/main.rs"
GENERATED = ROOT / "generated-pingora-proxy/src/main.rs"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def check_file(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    require("cache_eligible: Arc<AtomicUsize>" in text, f"cache_eligible field missing in {path}")
    require("cache_bypasses: Arc<AtomicUsize>" in text, f"cache_bypasses field missing in {path}")
    require("fn route_cache_enabled(route: &RouteConfig) -> bool" in text, f"route_cache_enabled missing in {path}")
    require("fn request_has_cache_bypass_headers(session: &Session, route: &RouteConfig) -> bool" in text, f"bypass helper missing in {path}")
    require("fn record_cache_gate(&self, session: &Session, route: &RouteConfig)" in text, f"record_cache_gate missing in {path}")
    require('method != "GET" && method != "HEAD"' in text, f"GET/HEAD gate missing in {path}")
    require('"authorization".to_string()' in text, f"authorization bypass missing in {path}")
    require('"x-api-key".to_string()' in text, f"x-api-key bypass missing in {path}")
    require("self.record_cache_gate(session, &route);" in text, f"record_cache_gate call missing in {path}")
    require('cache\\":{{\\"eligible' in text and '\\"bypasses' in text, f"runtime cache stats JSON missing in {path}")

    # Step 5A must not serve cached responses yet.
    forbidden = [
        "cache_store",
        "serve_cached",
        "insert_cache",
        "cache_hit",
        "cache_miss",
    ]
    for token in forbidden:
        require(token not in text, f"Full response cache behavior appeared too early: {token}")


def main() -> int:
    print("🧪 Verifying cache runtime gate counters")

    check_file(TEMPLATE)
    print("✅ Template cache gate counters correct")

    check_file(GENERATED)
    print("✅ Generated runtime cache gate counters correct")

    print("✅ CACHE RUNTIME GATE LOCAL VERIFIER PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
