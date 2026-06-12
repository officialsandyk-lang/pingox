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


def request(url: str, method: str = "GET") -> tuple[int, dict[str, str], str]:
    req = urllib.request.Request(url, data=b"" if method == "POST" else None, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read().decode("utf-8", errors="replace")


def wait_ready() -> dict:
    last = None
    for _ in range(30):
        try:
            status, _headers, body = request("http://127.0.0.1:8080/__gateway_runtime_state")
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

    for route in data.get("routes", []):
        if route.get("path") == "/api":
            route["cache"] = {
                "enabled": True,
                "ttl_seconds": 30,
                "methods": ["GET", "HEAD"],
                "cache_key": "method:path:query",
                "bypass_headers": ["authorization", "x-api-key"],
                "stale_while_revalidate_seconds": 0,
            }

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


def cache_state() -> dict:
    return wait_ready().get("cache", {})


def cache_header(headers: dict[str, str]) -> str | None:
    return headers.get("x-pingora-ai-cache")


def main() -> int:
    enable_api_cache()
    regenerate()

    run(["python", "-m", "pingora_ai.cli", "start", "--local"])
    before = wait_ready()
    print("before:", before.get("cache"))

    url = "http://127.0.0.1:8080/api/cache-invalidation-proof?x=1"

    s1, h1, _ = request(url)
    s2, h2, _ = request(url)

    print("first:", s1, cache_header(h1))
    print("second:", s2, cache_header(h2))

    if s1 != 200 or cache_header(h1) != "MISS":
        raise SystemExit("Expected first proof request to be MISS.")
    if s2 != 200 or cache_header(h2) != "HIT":
        raise SystemExit("Expected second proof request to be HIT.")

    populated = cache_state()
    print("populated:", populated)
    if populated.get("entries", 0) < 1:
        raise SystemExit(f"Expected cache entries > 0 before clear, got {populated}")

    clear_status, _clear_headers, clear_body = request(
        "http://127.0.0.1:8080/__gateway_runtime_admin/cache/clear",
        method="POST",
    )

    print("clear:", clear_status, clear_body)
    if clear_status != 200:
        raise SystemExit("Expected cache clear endpoint HTTP 200.")

    clear_data = json.loads(clear_body)
    if clear_data.get("ok") is not True or clear_data.get("cleared_entries", 0) < 1:
        raise SystemExit(f"Expected clear endpoint to clear entries, got {clear_data}")

    cleared = cache_state()
    print("cleared:", cleared)
    if cleared.get("entries", 0) != 0:
        raise SystemExit(f"Expected entries=0 after clear, got {cleared}")

    s3, h3, _ = request(url)
    print("third-after-clear:", s3, cache_header(h3))
    if s3 != 200 or cache_header(h3) != "MISS":
        raise SystemExit("Expected request after clear to be MISS again.")

    print("✅ CACHE INVALIDATION LOCAL VERIFICATION PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
