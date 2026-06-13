#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATE_URL = "http://127.0.0.1:8080/__gateway_runtime_state"


def fail(message: str) -> None:
    raise SystemExit(f"❌ {message}")


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=False)


def fetch_state() -> dict:
    with urllib.request.urlopen(STATE_URL, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> None:
    print("🧪 Verifying health policy config applies to local runtime")

    set_result = run([
        "pingora-ai",
        "health",
        "set",
        "--local",
        "--path",
        "/health",
        "--expected-status",
        "200",
        "--timeout-seconds",
        "3",
        "--interval-seconds",
        "7",
        "--healthy-threshold",
        "4",
        "--unhealthy-threshold",
        "5",
    ])

    if set_result.returncode != 0:
        print(set_result.stdout)
        print(set_result.stderr)
        fail("health set --local failed")

    config_path = ROOT / "generated-pingora-proxy" / "config.local.json"
    config = json.loads(config_path.read_text())

    from core.project_writer import write_project

    write_project(config, project_dir=ROOT / "generated-pingora-proxy")

    rust_path = ROOT / "generated-pingora-proxy" / "src" / "main.rs"
    rust = rust_path.read_text()

    required_rust = [
        'const HEALTH_CHECK_PATH: &str = "/health";',
        "const HEALTH_EXPECTED_STATUS: u16 = 200;",
        "const HEALTH_INTERVAL_SECONDS: u64 = 7;",
        "const HEALTH_TIMEOUT_SECONDS: u64 = 3;",
        "const HEALTHY_THRESHOLD: usize = 4;",
        "const UNHEALTHY_THRESHOLD: usize = 5;",
        '\\"expected_status\\":{}',
        "HEALTH_EXPECTED_STATUS,",
    ]

    missing = [item for item in required_rust if item not in rust]
    if missing:
        fail(f"generated Rust missing expected health policy markers: {missing}")

    start_result = run(["pingora-ai", "start", "--local"])
    if start_result.returncode != 0:
        print(start_result.stdout)
        print(start_result.stderr)
        fail("pingora-ai start --local failed")

    time.sleep(2)

    state = fetch_state()
    policy = state.get("active_health", {}).get("policy", {})

    expected = {
        "path": "/health",
        "expected_status": 200,
        "timeout_seconds": 3,
        "interval_seconds": 7,
        "healthy_threshold": 4,
        "unhealthy_threshold": 5,
    }

    for key, value in expected.items():
        if policy.get(key) != value:
            fail(f"runtime active_health.policy.{key} expected {value!r}, got {policy.get(key)!r}")

    upstreams = state.get("active_health", {}).get("upstreams", [])
    if not upstreams:
        fail("runtime active_health.upstreams is empty")

    unhealthy = [item for item in upstreams if item.get("healthy") is False]
    if unhealthy:
        fail(f"expected all demo upstreams healthy, unhealthy={unhealthy}")

    print("✅ Generated Rust constants reflect active_health config")
    print("✅ Runtime state reflects active_health config")
    print("✅ HEALTH POLICY RUNTIME APPLY LOCAL VERIFICATION PASSED")


if __name__ == "__main__":
    main()
