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
    print("🧪 Verifying local health policy reset CLI")

    result = run(["pingora-ai", "health", "reset", "--local"])

    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        fail("health reset --local failed")

    payload = json.loads(result.stdout)

    if payload.get("ok") is not True:
        fail("reset output ok was not true")

    if payload.get("reset") is not True:
        fail("reset output reset was not true")

    config_path = Path("generated-pingora-proxy") / "config.local.json"
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

    print("✅ health reset --local restored default active_health policy")
    print("✅ HEALTH POLICY RESET LOCAL VERIFICATION PASSED")


if __name__ == "__main__":
    main()
