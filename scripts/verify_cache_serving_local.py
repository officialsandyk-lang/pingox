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


def http_get(url: str, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], str]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, body


def wait_ready() -> dict:
    last = None
    for _ in range(30):
        try:
            status, _headers, body = http_get("http://127.0.0.1:8080/__gateway_runtime_state")
            last = body
            if status == 200:
                return json.loads(body)
        except Exception as exc:
            last = str(exc)
        time.sleep(1)

    raise SystemExit(f"Runtime not ready. Last response: {last}")


def enable_api_cache() -> None:
    path = CONFIG_LOCAL if CONFIG_LOCAL.exists() else CONFIG_BASE
    data = json.loads(path.read_text(encoding="utf-8"))

    found = False
    for route in data.get("routes", []):
        if route.get("path") == "/api":
            found = True
            route["cache"] = {
                "enabled": True,
                "ttl_seconds": 30,
                "methods": ["GET", "HEAD"],
                "cache_key": "method:path:query",
                "bypass_headers": ["authorization", "x-api-key"],
                "stale_while_revalidate_seconds": 0,
            }

    if not found:
        raise SystemExit("Could not find /api route to enable cache.")

    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"✅ enabled /api cache in {path.relative_to(ROOT)}")


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


def main() -> int:
    enable_api_cache()
    regenerate()

    run(["python", "-m", "pingora_ai.cli", "start", "--local"])
    before = wait_ready()
    print("before cache:", before.get("cache"))

    url = "http://127.0.0.1:8080/api/cache-step6-proof?x=verifier"

    first_status, first_headers, _first_body = http_get(url)
    second_status, second_headers, _second_body = http_get(url)
    after = wait_ready()

    first_cache = first_headers.get("x-pingora-ai-cache")
    second_cache = second_headers.get("x-pingora-ai-cache")
    cache = after.get("cache", {})

    print("first:", first_status, first_cache)
    print("second:", second_status, second_cache)
    print("after cache:", cache)

    if first_status != 200 or second_status != 200:
        raise SystemExit("Expected both cache proof requests to return HTTP 200.")

    if first_cache != "MISS":
        raise SystemExit(f"Expected first request MISS, got {first_cache!r}")

    if second_cache != "HIT":
        raise SystemExit(f"Expected second request HIT, got {second_cache!r}")

    if cache.get("hits", 0) < 1 or cache.get("misses", 0) < 1 or cache.get("entries", 0) < 1:
        raise SystemExit(f"Expected cache counters hits/misses/entries >= 1, got {cache}")

    print("✅ CACHE SERVING LOCAL VERIFICATION PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
