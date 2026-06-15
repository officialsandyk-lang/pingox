#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / "generated-pingora-proxy"


def fail(message: str) -> None:
    raise SystemExit(f"❌ {message}")


def ok(message: str) -> None:
    print(f"✅ {message}")


def warn(message: str) -> None:
    print(f"⚠️  {message}")


def run(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )


def load_json(path: Path) -> dict:
    if not path.exists():
        fail(f"Missing file: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"Invalid JSON in {path}: {exc}")


def http_status(url: str, headers: dict[str, str] | None = None, timeout: int = 10) -> int:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def verify_docker_config_contract() -> None:
    docker_config = GENERATED / "config.docker.json"
    local_config = GENERATED / "config.local.json"

    docker = load_json(docker_config)
    local = load_json(local_config)

    docker_text = json.dumps(docker)
    local_text = json.dumps(local)

    if "host.docker.internal" not in docker_text:
        warn("config.docker.json does not contain host.docker.internal; verify Docker addressing is intentional")
    else:
        ok("Docker config uses Docker host addressing")

    if "host.docker.internal" in local_text:
        fail("Local config leaks Docker host addressing")

    ok("Docker and local configs are separated")


def verify_port_policy_contract() -> None:
    files_to_scan = [
        ROOT / "core" / "ports.py",
        ROOT / "core" / "platforms" / "docker_host.py",
        ROOT / "pingora_ai" / "cli.py",
    ]

    text = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in files_to_scan if path.exists())

    if "80" not in text:
        warn("Could not confirm Docker public HTTP port policy from scanned files")
    else:
        ok("Docker production-style public port policy markers present")

    if "8443" in text:
        warn("Legacy 8443 marker found; acceptable only as legacy/internal compatibility")

    ok("Port policy contract scanned")


def verify_repair_safety_contract() -> None:
    control_api = (ROOT / "pingora_ai" / "control_api.py").read_text(encoding="utf-8", errors="replace")

    required = [
        "/__gateway_control/agent/repair-plan/dry-run",
        "/__gateway_control/agent/repair-plan/history",
        "will_execute",
        "dry_run_only",
        "docker_apply",
        "linux_deploy",
        "runtime_mutation",
    ]

    missing = [marker for marker in required if marker not in control_api]
    if missing:
        fail(f"Missing dry-run repair safety markers: {missing}")

    forbidden = [
        "/__gateway_control/agent/repair/execute",
        "/__gateway_control/agent/repair/live",
        "docker_apply\": true",
        "linux_deploy\": true",
    ]

    found = [marker for marker in forbidden if marker in control_api]
    if found:
        fail(f"Unexpected live repair markers found: {found}")

    ok("Repair agent remains dry-run only")


def verify_production_hardening_gate() -> None:
    script = ROOT / "scripts" / "verify_production_hardening_foundation.py"
    if not script.exists():
        fail("Production hardening verifier missing")

    result = run([sys.executable, str(script.relative_to(ROOT))], timeout=360)
    print(result.stdout)

    if result.returncode != 0:
        fail("Production hardening foundation verifier failed")

    ok("Production hardening foundation still passes")


def verify_docker_live() -> None:
    docker_check = run(["docker", "ps"], timeout=30)
    if docker_check.returncode != 0:
        fail(f"Docker is not available or not running:\n{docker_check.stdout}")

    ok("Docker daemon is reachable")

    status = run(["pingora-ai", "status", "--docker"], timeout=120)
    print(status.stdout)

    if status.returncode != 0:
        fail("pingora-ai status --docker failed")

    if "NAMES" in status.stdout and "STATUS" in status.stdout and "PORTS" in status.stdout:
        lines = [line.strip() for line in status.stdout.splitlines() if line.strip()]
        container_lines = [
            line for line in lines
            if not line.startswith("NAMES")
            and not line.startswith("Docker containers")
            and not line.startswith("Return code")
            and not line.startswith("STDOUT")
            and "HTTP status" not in line
        ]
        if not container_lines:
            fail("Docker live check requested, but no Docker gateway containers are running")

    if "200" not in status.stdout and "healthy" not in status.stdout.lower():
        fail("Docker live check requested, but Docker status did not show HTTP 200/healthy marker")

    ok("Docker status command completed with live health evidence")

    # Keep this conservative: do not deploy or mutate.
    for url in [
        "http://127.0.0.1/",
        "http://127.0.0.1/__gateway_runtime_state",
    ]:
        try:
            status_code = http_status(url)
            if status_code not in {200, 301, 302, 307, 308, 401, 403}:
                fail(f"Docker edge URL returned unexpected status {status_code}: {url}")
            ok(f"Docker edge reachable: {url} -> {status_code}")
        except Exception as exc:
            fail(f"Docker live URL check failed for {url}: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Run live Docker status/edge checks. Does not deploy.")
    args = parser.parse_args()

    print("🧪 Verifying Docker production profile")

    verify_docker_config_contract()
    verify_port_policy_contract()
    verify_repair_safety_contract()
    verify_production_hardening_gate()

    if args.live:
        verify_docker_live()
    else:
        ok("Live Docker checks skipped. Use --live to check running Docker without deploying.")

    print("\n✅ DOCKER PRODUCTION PROFILE VERIFICATION PASSED")


if __name__ == "__main__":
    main()
