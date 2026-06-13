#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
from pathlib import Path


def fail(message: str) -> None:
    raise SystemExit(f"❌ {message}")


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=False)


def main() -> None:
    print("🧪 Verifying local health policy config CLI")

    result = run([
        "pingora-ai",
        "health",
        "set",
        "--local",
        "--path",
        "/health",
        "--expected-status",
        "200",
        "--timeout-seconds",
        "2",
        "--interval-seconds",
        "5",
        "--healthy-threshold",
        "2",
        "--unhealthy-threshold",
        "3",
    ])

    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        fail("health set --local failed")

    try:
        payload = json.loads(result.stdout)
    except Exception as exc:
        print(result.stdout)
        fail(f"health set output was not JSON: {exc}")

    if payload.get("ok") is not True:
        fail("health set output ok was not true")

    config_path = Path("generated-pingora-proxy") / "config.local.json"
    if not config_path.exists():
        fail("config.local.json was not written")

    config = json.loads(config_path.read_text())
    policy = config.get("active_health") or {}

    expected = {
        "path": "/health",
        "expected_status": 200,
        "timeout_seconds": 2,
        "interval_seconds": 5,
        "healthy_threshold": 2,
        "unhealthy_threshold": 3,
    }

    for key, value in expected.items():
        if policy.get(key) != value:
            fail(f"active_health.{key} expected {value!r}, got {policy.get(key)!r}")

    print("✅ health set --local wrote active_health policy")
    print("✅ HEALTH POLICY CONFIG LOCAL VERIFICATION PASSED")


if __name__ == "__main__":
    main()
