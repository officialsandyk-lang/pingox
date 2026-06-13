#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATE_URL = "http://127.0.0.1:8080/__gateway_runtime_state"
TARGET_PORT = 9102
TARGET_UPSTREAM = f"127.0.0.1:{TARGET_PORT}"


def fail(message: str) -> None:
    raise SystemExit(f"❌ {message}")


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=False)


def fetch_state() -> dict:
    with urllib.request.urlopen(STATE_URL, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def upstream_rows() -> list[dict]:
    state = fetch_state()
    return [
        item
        for item in state.get("active_health", {}).get("upstreams", [])
        if item.get("upstream") == TARGET_UPSTREAM
    ]


def wait_for_condition(label: str, predicate, timeout_seconds: int = 45) -> list[dict]:
    deadline = time.time() + timeout_seconds
    last_rows: list[dict] = []

    while time.time() < deadline:
        rows = upstream_rows()
        last_rows = rows

        if rows and predicate(rows):
            print(f"✅ {label}")
            return rows

        time.sleep(2)

    print(json.dumps(last_rows, indent=2))
    fail(f"Timed out waiting for: {label}")


def kill_backend() -> None:
    result = run(["bash", "-lc", f"lsof -ti :{TARGET_PORT} | xargs kill -9 2>/dev/null || true"])
    if result.returncode not in (0, 123):
        print(result.stdout)
        print(result.stderr)
        fail(f"failed to stop backend {TARGET_PORT}")


def start_backend() -> None:
    # Use the proven local start path so missing demo backends are restored reliably.
    result = run(["pingora-ai", "start", "--local"])
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        fail("failed to restart local runtime/demo backends")

    time.sleep(5)


def main() -> None:
    print("🧪 Verifying active health failure and recovery locally")

    print("✅ Ensuring local gateway is running")
    start = run(["pingora-ai", "start", "--local"])
    if start.returncode != 0:
        print(start.stdout)
        print(start.stderr)
        fail("pingora-ai start --local failed")

    wait_for_condition(
        f"{TARGET_UPSTREAM} starts healthy",
        lambda rows: all(item.get("healthy") is True for item in rows),
        timeout_seconds=30,
    )

    print(f"🛑 Stopping backend {TARGET_UPSTREAM}")
    kill_backend()

    failed_rows = wait_for_condition(
        f"{TARGET_UPSTREAM} becomes unhealthy",
        lambda rows: all(item.get("healthy") is False for item in rows),
        timeout_seconds=60,
    )

    for row in failed_rows:
        if int(row.get("consecutive_failures") or 0) < 1:
            fail("unhealthy row did not record consecutive failures")

    print(f"🚀 Restarting backend {TARGET_UPSTREAM}")
    start_backend()

    recovered_rows = wait_for_condition(
        f"{TARGET_UPSTREAM} recovers healthy",
        lambda rows: all(item.get("healthy") is True for item in rows),
        timeout_seconds=60,
    )

    for row in recovered_rows:
        if int(row.get("consecutive_successes") or 0) < 1:
            fail("recovered row did not record consecutive successes")

    print("✅ Active health detected backend failure")
    print("✅ Active health detected backend recovery")
    print("✅ ACTIVE HEALTH FAILURE RECOVERY LOCAL VERIFICATION PASSED")


if __name__ == "__main__":
    main()
