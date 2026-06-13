#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run_json(cmd: list[str]) -> dict:
    print("$ " + " ".join(cmd))
    result = subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    print(result.stdout)

    if result.returncode != 0:
        raise SystemExit(f"❌ Command failed: {' '.join(cmd)}")

    try:
        return json.loads(result.stdout)
    except Exception as exc:
        raise SystemExit(f"❌ Output was not JSON: {exc}")


def main() -> None:
    print("🧪 Verifying local health CLI")

    listed = run_json(["pingora-ai", "health", "list", "--local"])

    if not listed.get("ok"):
        raise SystemExit("❌ health list returned ok=false")

    policy = listed.get("policy", {})
    upstreams = listed.get("upstreams", [])

    if not isinstance(policy, dict) or not policy:
        raise SystemExit("❌ health list missing policy")

    if not isinstance(upstreams, list) or not upstreams:
        raise SystemExit("❌ health list missing upstreams")

    for key in ["path", "expected_status", "timeout_seconds", "healthy_threshold", "unhealthy_threshold"]:
        if key not in policy:
            raise SystemExit(f"❌ health list policy missing {key}")

    tested = run_json(["pingora-ai", "health", "test", "--local"])

    results = tested.get("results", [])
    if not isinstance(results, list) or not results:
        raise SystemExit("❌ health test missing results")

    if tested.get("summary", {}).get("failed", 0) != 0:
        raise SystemExit("❌ health test has failed upstream checks")

    print("✅ HEALTH CLI LOCAL VERIFICATION PASSED")


if __name__ == "__main__":
    main()
