from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = "http://127.0.0.1:8080"
CONTROL = "http://127.0.0.1:8787"
TOKEN = "dev-local-control-token"


def run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, cwd=ROOT, text=True)
    if result.returncode != 0:
        raise SystemExit(f"Command failed: {' '.join(cmd)}")


def request(url: str, method: str = "GET", token: bool = False) -> tuple[int, dict[str, str], str]:
    headers = {"X-Pingora-Control-Token": TOKEN} if token else {}
    req = urllib.request.Request(url, data=b"" if method == "POST" else None, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read().decode("utf-8", errors="replace")


def get_json(url: str, token: bool = False) -> dict[str, Any]:
    status, _headers, body = request(url, token=token)
    if status != 200:
        raise SystemExit(f"Expected 200 from {url}, got {status}: {body}")
    return json.loads(body)


def wait_ready() -> dict[str, Any]:
    last = None
    for _ in range(30):
        try:
            status, _headers, body = request(f"{RUNTIME}/__gateway_runtime_state")
            last = body
            if status == 200:
                return json.loads(body)
        except Exception as exc:
            last = str(exc)
        time.sleep(1)
    raise SystemExit(f"Runtime not ready. Last response: {last}")


def cache_state() -> dict[str, Any]:
    return wait_ready().get("cache", {})


def cache_header(headers: dict[str, str]) -> str | None:
    return headers.get("x-pingora-ai-cache")


def ensure_cache_populated() -> None:
    url = f"{RUNTIME}/api/dashboard-cache-control-proof?x=1"
    s1, h1, _ = request(url)
    s2, h2, _ = request(url)

    print("populate first:", s1, cache_header(h1))
    print("populate second:", s2, cache_header(h2))

    if s1 != 200 or cache_header(h1) != "MISS":
        raise SystemExit("Expected first populate request MISS.")
    if s2 != 200 or cache_header(h2) != "HIT":
        raise SystemExit("Expected second populate request HIT.")

    state = cache_state()
    print("populated cache:", state)
    if state.get("entries", 0) < 1:
        raise SystemExit(f"Expected cache entries > 0, got {state}")


def main() -> int:
    print("🧪 Verifying cache dashboard control local")

    run(["python", "-m", "pingora_ai.cli", "start", "--local"])
    wait_ready()

    html_status, _html_headers, html = request(f"{RUNTIME}/")
    if html_status != 200:
        raise SystemExit("Dashboard not reachable.")
    if "Clear Cache Dry Run" not in html or "Clear Cache Live" not in html:
        raise SystemExit("Dashboard missing cache clear buttons.")
    print("✅ Dashboard cache clear buttons present")

    ensure_cache_populated()

    dry_run = get_json(f"{CONTROL}/__gateway_control/cache/clear/dry-run", token=True)
    print("dry-run:", dry_run)
    if dry_run.get("agent") != "cache_control_agent":
        raise SystemExit("Dry run wrong agent.")
    if dry_run.get("mode") != "dry_run_only":
        raise SystemExit("Dry run wrong mode.")
    if dry_run.get("will_not_execute") is not True:
        raise SystemExit("Dry run should not execute.")
    if dry_run.get("would_clear_cache") is not True:
        raise SystemExit("Dry run should report would_clear_cache.")
    if cache_state().get("entries", 0) < 1:
        raise SystemExit("Dry run unexpectedly cleared cache.")
    print("✅ Dry-run did not clear cache")

    blocked = get_json(f"{CONTROL}/__gateway_control/cache/clear", token=True)
    print("blocked:", blocked)
    if blocked.get("ok") is not False or blocked.get("decision") != "blocked":
        raise SystemExit("Unconfirmed clear should be blocked.")
    if cache_state().get("entries", 0) < 1:
        raise SystemExit("Blocked clear unexpectedly cleared cache.")
    print("✅ Unconfirmed live clear blocked")

    confirmed = get_json(f"{CONTROL}/__gateway_control/cache/clear?confirm=true", token=True)
    print("confirmed:", confirmed)
    if confirmed.get("ok") is not True:
        raise SystemExit(f"Confirmed cache clear failed: {confirmed}")
    if confirmed.get("live_runtime_mutation") is not True:
        raise SystemExit("Confirmed cache clear did not report live runtime mutation.")
    if int(confirmed.get("cleared_entries") or 0) < 1:
        raise SystemExit("Confirmed cache clear did not clear entries.")

    cleared = cache_state()
    print("cleared cache:", cleared)
    if cleared.get("entries", 0) != 0:
        raise SystemExit(f"Expected entries=0 after confirmed clear, got {cleared}")

    s3, h3, _ = request(f"{RUNTIME}/api/dashboard-cache-control-proof?x=1")
    print("after-clear:", s3, cache_header(h3))
    if s3 != 200 or cache_header(h3) != "MISS":
        raise SystemExit("Expected request after dashboard clear to be MISS.")

    print("✅ CACHE DASHBOARD CONTROL LOCAL VERIFICATION PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
