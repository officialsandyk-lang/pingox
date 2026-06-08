from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PYTHON = sys.executable

from core.security_policy_contract import (
    apply_security_policy_contract,
    verify_security_policy_contract,
)


def section(title: str) -> None:
    print("")
    print("=" * 90)
    print(title)
    print("=" * 90)


def run(command: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    print("")
    print("$ " + " ".join(command))

    result = subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        text=True,
        capture_output=True,
        timeout=1800,
    )

    if result.stdout.strip():
        print(result.stdout[-8000:])

    if result.stderr.strip():
        print(result.stderr[-4000:])

    if check and result.returncode != 0:
        raise AssertionError(f"Command failed: {' '.join(command)}")

    return result


def baseline_config() -> dict:
    return {
        "runtime": "local",
        "strategy": "direct",
        "port": 8090,
        "routes": [
            {
                "path": "/",
                "type": "static",
                "route_type": "static",
                "root": "public",
                "index": "index.html",
                "upstreams": [],
            },
            {
                "path": "/api",
                "type": "proxy",
                "route_type": "proxy",
                "balancing": "random",
                "upstreams": [
                    "127.0.0.1:9101",
                    "127.0.0.1:9102",
                    "127.0.0.1:9103",
                ],
            },
        ],
    }


def assert_ok(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"✅ {name}")


def main() -> int:
    section("Unit contract checks")

    raw = baseline_config()
    missing = verify_security_policy_contract(raw)
    assert_ok("missing contract fails verifier", missing["ok"] is False)

    applied = apply_security_policy_contract(raw, environment="local")
    local_result = verify_security_policy_contract(applied)
    assert_ok("applied local contract passes verifier", local_result["ok"] is True)

    strict_result = verify_security_policy_contract(applied, strict=True)
    assert_ok("strict production gate fails until real TLS/auth/secrets/container hardening are enabled", strict_result["ok"] is False)

    route = next(r for r in applied["routes"] if r["path"] == "/api")
    assert_ok("route has allowed methods", bool(route.get("allowed_methods")))
    assert_ok("route has CRUD policy", bool(route.get("crud_policy")))
    assert_ok("config has HTTPS contract", bool(applied["security"]["https"]))
    assert_ok("config has WAF contract", bool(applied["security"]["waf"]))
    assert_ok("config has auth contract", bool(applied["security"]["auth"]))
    assert_ok("config has secrets contract", bool(applied["security"]["secrets"]))
    assert_ok("config has container hardening contract", bool(applied["security"]["container_security"]))

    section("Current generated config check")

    generated = PROJECT_ROOT / "generated-pingora-proxy" / "config.json"

    if generated.exists():
        config = json.loads(generated.read_text(encoding="utf-8"))
        result = verify_security_policy_contract(config)
        assert_ok("current generated config has security contract", result["ok"] is True)
    else:
        print("ℹ️ No generated config found; skipping runtime config check.")

    print("")
    print("✅ SECURITY CONTRACT STRESS TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
