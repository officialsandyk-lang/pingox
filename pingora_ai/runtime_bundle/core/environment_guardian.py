from __future__ import annotations

import json
import shutil
import socket
import subprocess
import urllib.request
from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class EnvironmentCheck:
    name: str
    ok: bool
    details: dict[str, Any]


def _run(cmd: list[str], timeout: int = 10) -> dict[str, Any]:
    try:
        p = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
        return {
            "cmd": cmd,
            "returncode": p.returncode,
            "stdout": p.stdout[-3000:],
            "stderr": p.stderr[-3000:],
        }
    except Exception as exc:
        return {"cmd": cmd, "returncode": -1, "stdout": "", "stderr": str(exc)}


def _http(url: str, timeout: int = 5, headers: dict[str, str] | None = None) -> dict[str, Any]:
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(400).decode("utf-8", errors="replace")
            return {"url": url, "status": r.status, "body": body, "headers": dict(r.headers)}
    except Exception as exc:
        return {"url": url, "status": None, "body": str(exc), "headers": {}}


def check_command_exists(command: str) -> EnvironmentCheck:
    ok = shutil.which(command) is not None
    return EnvironmentCheck(
        name=f"command_{command}_exists",
        ok=ok,
        details={"command": command, "path": shutil.which(command)},
    )


def check_nginx() -> list[EnvironmentCheck]:
    checks: list[EnvironmentCheck] = []

    checks.append(check_command_exists("nginx"))

    status = _run(["systemctl", "is-active", "nginx"])
    checks.append(EnvironmentCheck(
        name="nginx_active",
        ok=status["returncode"] == 0 and "active" in status["stdout"],
        details=status,
    ))

    listen = _run(["bash", "-lc", "ss -ltnp | grep -E ':80|:443' || true"])
    checks.append(EnvironmentCheck(
        name="nginx_listening_public_ports",
        ok=":80" in listen["stdout"] or ":443" in listen["stdout"],
        details=listen,
    ))

    return checks


def check_iptables_public_edge() -> EnvironmentCheck:
    table = _run(["bash", "-lc", "sudo iptables -L INPUT -n --line-numbers | sed -n '1,80p'"], timeout=10)
    stdout = table.get("stdout", "")

    reject_index = None
    port80_index = None
    port443_index = None

    for line in stdout.splitlines():
        parts = line.split()
        if not parts or not parts[0].isdigit():
            continue
        idx = int(parts[0])
        if "REJECT" in line and reject_index is None:
            reject_index = idx
        if "dpt:80" in line and port80_index is None:
            port80_index = idx
        if "dpt:443" in line and port443_index is None:
            port443_index = idx

    ok80 = port80_index is not None and (reject_index is None or port80_index < reject_index)
    ok443 = port443_index is not None and (reject_index is None or port443_index < reject_index)

    return EnvironmentCheck(
        name="iptables_80_443_before_reject",
        ok=ok80 and ok443,
        details={
            "port80_index": port80_index,
            "port443_index": port443_index,
            "reject_index": reject_index,
            "iptables": stdout,
        },
    )


def check_public_edge(base_url: str = "http://127.0.0.1") -> list[EnvironmentCheck]:
    checks: list[EnvironmentCheck] = []
    home = _http(base_url.rstrip("/") + "/")
    checks.append(EnvironmentCheck(
        name="public_edge_homepage",
        ok=home["status"] == 200,
        details=home,
    ))

    env = _http(base_url.rstrip("/") + "/.env")
    checks.append(EnvironmentCheck(
        name="public_edge_blocks_env",
        ok=env["status"] in {401, 403, 404},
        details=env,
    ))

    wp = _http(base_url.rstrip("/") + "/wp-admin")
    checks.append(EnvironmentCheck(
        name="public_edge_blocks_wp_admin",
        ok=wp["status"] in {401, 403, 404},
        details=wp,
    ))

    return checks


def check_planner_api(api_url: str | None, token: str | None = None) -> list[EnvironmentCheck]:
    if not api_url:
        return [EnvironmentCheck(
            name="planner_api_configured",
            ok=False,
            details={"reason": "PINGORA_AI_API_URL/PINGORA_AI_API_BASE_URL is not set"},
        )]

    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-API-Key"] = token

    health = _http(api_url.rstrip("/") + "/health", headers=headers)
    return [EnvironmentCheck(
        name="planner_api_health",
        ok=health["status"] == 200,
        details=health,
    )]


def scan_environment(
    environment: str = "local",
    public_url: str | None = None,
    api_url: str | None = None,
    api_token: str | None = None,
) -> dict[str, Any]:
    checks: list[EnvironmentCheck] = []

    checks.append(check_command_exists("curl"))
    checks.extend(check_nginx())
    checks.append(check_iptables_public_edge())

    if public_url:
        checks.extend(check_public_edge(public_url))
    else:
        checks.extend(check_public_edge("http://127.0.0.1"))

    checks.extend(check_planner_api(api_url, api_token))

    failed = [c for c in checks if not c.ok]

    return {
        "environment": environment,
        "success": not failed,
        "checks": [asdict(c) for c in checks],
        "failed_checks": [asdict(c) for c in failed],
    }


def main() -> int:
    import argparse
    import os

    p = argparse.ArgumentParser()
    p.add_argument("--environment", default="local")
    p.add_argument("--public-url", default=None)
    p.add_argument("--api-url", default=os.getenv("PINGORA_AI_API_BASE_URL") or os.getenv("PINGORA_AI_API_URL"))
    p.add_argument("--api-token", default=os.getenv("PINGORA_AI_API_TOKEN"))
    args = p.parse_args()

    result = scan_environment(args.environment, args.public_url, args.api_url, args.api_token)
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
