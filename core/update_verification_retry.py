from __future__ import annotations

import json
import time
import urllib.request
from typing import Any

from core import environment_deployer as env


def _route_paths(config: dict[str, Any]) -> list[str]:
    paths: list[str] = []

    for route in config.get("routes", []) or []:
        if not isinstance(route, dict):
            continue

        path = str(route.get("path") or "/").strip() or "/"

        if not path.startswith("/"):
            path = "/" + path

        paths.append(path)

    return sorted(set(paths), key=lambda p: (p != "/", p))


def _warm_routes(live_url: str, config: dict[str, Any], rounds: int = 3) -> None:
    base = live_url.rstrip("/")

    for _ in range(rounds):
        for path in _route_paths(config):
            route_path = path

            if path != "/" and not path.endswith("/"):
                route_path = path + "/"

            try:
                req = urllib.request.Request(
                    base + route_path,
                    method="GET",
                    headers={"User-Agent": "ai-pingora-update-warmup"},
                )
                with urllib.request.urlopen(req, timeout=5) as response:
                    response.read(256)
            except Exception:
                pass


def verify_deployment_with_retry(
    config: dict[str, Any],
    live_url: str,
    level: str = "full",
    attempts: int = 3,
    first_wait_seconds: int = 25,
    retry_wait_seconds: int = 15,
) -> dict[str, Any]:
    last: dict[str, Any] = {
        "success": False,
        "error": "verification_not_run",
    }

    for attempt in range(1, attempts + 1):
        wait = first_wait_seconds if attempt == 1 else retry_wait_seconds

        print("")
        print(f"🧪 Docker update verification attempt {attempt}/{attempts}")
        print(f"⏳ Waiting {wait}s for Docker/Nginx/backends to settle...")
        time.sleep(wait)

        print("🔥 Warming routes before verification...")
        _warm_routes(live_url, config, rounds=3)

        try:
            verification = env.verify_deployment(
                config,
                live_url=live_url,
                level=level,
            )
        except Exception as exc:
            verification = {
                "success": False,
                "error": repr(exc),
                "failure_summary": repr(exc),
            }

        last = verification if isinstance(verification, dict) else {
            "success": False,
            "raw_verification": str(verification),
        }

        if last.get("success"):
            print("✅ Docker update verification passed after retry wrapper")
            return last

        failed = []
        for check in last.get("checks", []) or []:
            if isinstance(check, dict) and not check.get("ok"):
                failed.append(check.get("name"))

        summary = failed or last.get("failure_summary") or last.get("error") or json.dumps(last, default=str)[:500]
        print(f"⚠️ Docker update verification attempt {attempt} failed: {summary}")

    print("❌ Docker update verification failed after retry wrapper")
    return last
