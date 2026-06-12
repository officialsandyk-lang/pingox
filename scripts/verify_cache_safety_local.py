from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_LOCAL = ROOT / "generated-pingora-proxy" / "config.local.json"
CONFIG_BASE = ROOT / "generated-pingora-proxy" / "config.json"


def run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, cwd=ROOT, text=True)
    if result.returncode != 0:
        raise SystemExit(f"Command failed: {' '.join(cmd)}")


def http_request(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], str]:
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, text
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, text


def wait_ready() -> dict:
    last = None

    for _ in range(30):
        try:
            status, _headers, body = http_request("http://127.0.0.1:8080/__gateway_runtime_state")
            last = body
            if status == 200:
                return json.loads(body)
        except Exception as exc:
            last = str(exc)

        time.sleep(1)

    raise SystemExit(f"Runtime not ready. Last response: {last}")


def enable_api_cache(ttl_seconds: int = 2) -> None:
    path = CONFIG_LOCAL if CONFIG_LOCAL.exists() else CONFIG_BASE
    data = json.loads(path.read_text(encoding="utf-8"))

    found = False
    for route in data.get("routes", []):
        if route.get("path") == "/api":
            found = True
            route["cache"] = {
                "enabled": True,
                "ttl_seconds": ttl_seconds,
                "methods": ["GET", "HEAD"],
                "cache_key": "method:path:query",
                "bypass_headers": ["authorization", "x-api-key"],
                "stale_while_revalidate_seconds": 0,
            }

    if not found:
        raise SystemExit("Could not find /api route to enable cache.")

    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"✅ enabled /api cache with ttl={ttl_seconds}s in {path.relative_to(ROOT)}")


def regenerate() -> None:
    code = """
from pathlib import Path
import json
from core.project_writer import write_project

config_path = Path("generated-pingora-proxy/config.local.json")
if not config_path.exists():
    config_path = Path("generated-pingora-proxy/config.json")

config = json.loads(config_path.read_text(encoding="utf-8"))
write_project(config)
"""
    run(["python", "-c", code])
    run(["cargo", "check", "--manifest-path", "generated-pingora-proxy/Cargo.toml"])


def cache_header(headers: dict[str, str]) -> str | None:
    return headers.get("x-pingora-ai-cache")


def assert_cache_pair(url: str, label: str) -> None:
    first_status, first_headers, _ = http_request(url)
    second_status, second_headers, _ = http_request(url)

    first_cache = cache_header(first_headers)
    second_cache = cache_header(second_headers)

    print(label, "first:", first_status, first_cache)
    print(label, "second:", second_status, second_cache)

    if first_status != 200 or second_status != 200:
        raise SystemExit(f"{label}: expected HTTP 200 for both requests.")

    if first_cache != "MISS":
        raise SystemExit(f"{label}: expected first request MISS, got {first_cache!r}")

    if second_cache != "HIT":
        raise SystemExit(f"{label}: expected second request HIT, got {second_cache!r}")


def assert_bypass(url: str, label: str, headers: dict[str, str] | None = None, method: str = "GET") -> None:
    status1, headers1, _ = http_request(url, method=method, headers=headers, body=b"{}" if method != "GET" else None)
    status2, headers2, _ = http_request(url, method=method, headers=headers, body=b"{}" if method != "GET" else None)

    cache1 = cache_header(headers1)
    cache2 = cache_header(headers2)

    print(label, "first:", status1, cache1)
    print(label, "second:", status2, cache2)

    if status1 not in (200, 201, 204, 405) or status2 not in (200, 201, 204, 405):
        raise SystemExit(f"{label}: unexpected statuses {status1}, {status2}")

    if cache1 == "HIT" or cache2 == "HIT":
        raise SystemExit(f"{label}: expected bypass/no HIT, got {cache1!r}, {cache2!r}")


def main() -> int:
    enable_api_cache(ttl_seconds=2)
    regenerate()

    run(["python", "-m", "pingora_ai.cli", "start", "--local"])
    before = wait_ready()
    print("before cache:", before.get("cache"))

    base = "http://127.0.0.1:8080/api/cache-safety-proof"

    assert_cache_pair(f"{base}?case=normal", "normal-cache")

    assert_bypass(
        f"{base}?case=authorization-bypass",
        "authorization-bypass",
        headers={"Authorization": "Bearer verifier-token"},
    )

    assert_bypass(
        f"{base}?case=x-api-key-bypass",
        "x-api-key-bypass",
        headers={"X-API-Key": "verifier-key"},
    )

    assert_bypass(
        f"{base}?case=post-bypass",
        "post-bypass",
        method="POST",
        headers={"Content-Type": "application/json"},
    )

    assert_cache_pair(f"{base}?case=query-a", "query-key-a")
    assert_cache_pair(f"{base}?case=query-b", "query-key-b")

    ttl_url = f"{base}?case=ttl-expiry"
    first_status, first_headers, _ = http_request(ttl_url)
    second_status, second_headers, _ = http_request(ttl_url)

    print("ttl-before-expiry first:", first_status, cache_header(first_headers))
    print("ttl-before-expiry second:", second_status, cache_header(second_headers))

    if cache_header(first_headers) != "MISS" or cache_header(second_headers) != "HIT":
        raise SystemExit("TTL setup failed: expected MISS then HIT before expiry.")

    time.sleep(3)

    third_status, third_headers, _ = http_request(ttl_url)
    third_cache = cache_header(third_headers)

    print("ttl-after-expiry third:", third_status, third_cache)

    if third_status != 200:
        raise SystemExit("TTL expiry request did not return HTTP 200.")

    if third_cache != "MISS":
        raise SystemExit(f"Expected TTL expiry request MISS, got {third_cache!r}")

    after = wait_ready()
    print("after cache:", after.get("cache"))

    cache = after.get("cache", {})
    if cache.get("hits", 0) < 3 or cache.get("misses", 0) < 5:
        raise SystemExit(f"Expected safety verifier cache counters to increase, got {cache}")

    print("✅ CACHE SAFETY LOCAL VERIFICATION PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
