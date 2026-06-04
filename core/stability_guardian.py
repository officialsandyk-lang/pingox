from __future__ import annotations

import json
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Allow running this file directly: python core/stability_guardian.py
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@dataclass
class StabilityCheck:
    name: str
    ok: bool
    details: dict[str, Any]


def _http_status(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    insecure_tls: bool = False,
    timeout: int = 5,
) -> tuple[int | None, str, dict[str, str]]:
    try:
        req = urllib.request.Request(url, headers=headers or {})
        ctx = ssl._create_unverified_context() if insecure_tls else None
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as response:
            body = response.read(5000).decode("utf-8", "ignore")
            return int(response.status), body, dict(response.headers)
    except urllib.error.HTTPError as exc:
        body = exc.read(5000).decode("utf-8", "ignore")
        return int(exc.code), body, dict(exc.headers)
    except Exception as exc:
        return None, str(exc), {}


def _run(cmd: list[str], *, timeout: int = 300) -> dict[str, Any]:
    result = subprocess.run(
        cmd,
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    return {
        "cmd": cmd,
        "returncode": result.returncode,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
        "ok": result.returncode == 0,
    }


def _load_config() -> dict[str, Any]:
    path = PROJECT_ROOT / "generated-pingora-proxy" / "config.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _route_map(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(route.get("path")): route
        for route in config.get("routes", [])
        if isinstance(route, dict) and route.get("path")
    }




def detect_active_environment() -> dict[str, Any]:
    """
    Best-effort local active environment detector.

    local:
      http://127.0.0.1:8090/__gateway_runtime_state returns 200

    docker:
      http://127.0.0.1:8090/__edge_health returns 200
      https://127.0.0.1:8443/ returns 200

    bare-metal is remote and checked only when --environment linux is used.
    """
    docker_edge_status, _, _ = _http_status("http://127.0.0.1:8090/__edge_health")
    docker_https_status, _, _ = _http_status("https://127.0.0.1:8443/", insecure_tls=True)
    local_state_status, _, _ = _http_status("http://127.0.0.1:8090/__gateway_runtime_state")

    detected = []

    if docker_edge_status == 200 and docker_https_status == 200:
        detected.append("docker")

    if local_state_status == 200 and docker_https_status != 200:
        detected.append("local")

    return {
        "detected": detected,
        "docker_edge_status": docker_edge_status,
        "docker_https_status": docker_https_status,
        "local_runtime_state_status": local_state_status,
    }


def active_environment_check(expected: str) -> StabilityCheck:
    if expected == "linux":
        return StabilityCheck(
            name="active_environment",
            ok=True,
            details={
                "skipped": True,
                "reason": "Bare-metal is remote and checked by host URL.",
            },
        )

    active = detect_active_environment()
    detected = set(active.get("detected") or [])

    ok = expected in detected

    return StabilityCheck(
        name="active_environment",
        ok=ok,
        details={
            "expected": expected,
            **active,
            "hint": None if ok else f"{expected} does not appear to be the active local environment.",
        },
    )


def check_config_integrity(expected_runtime: str | None = None) -> StabilityCheck:
    config = _load_config()
    routes = _route_map(config)
    failures: list[str] = []

    if expected_runtime:
        runtime = config.get("runtime") or config.get("runtime_mode")
        if runtime != expected_runtime:
            failures.append(f"runtime mismatch: expected {expected_runtime}, got {runtime}")

    root = routes.get("/")
    if not root:
        failures.append("missing / route")
    else:
        root_type = root.get("type") or root.get("route_type")
        if root_type != "static":
            failures.append(f"/ must be static, got {root_type}")

    for path, route in routes.items():
        route_type = route.get("type") or route.get("route_type")
        algo = route.get("balancing") or route.get("algorithm") or "single"

        upstreams = route.get("upstreams") or []
        if not upstreams and route.get("upstream"):
            upstreams = [route.get("upstream")]

        # Infer proxy if older/update-generated route has upstreams but no explicit type.
        if not route_type and upstreams:
            route_type = "proxy"

        if path == "/":
            continue

        if route_type != "proxy":
            failures.append(f"{path} must be proxy, got {route_type}")
            continue

        if not upstreams:
            failures.append(f"{path} has no upstreams")

        valid_algorithms = {
            "single",
            "random",
            "round_robin",
            "weighted_round_robin",
            "ip_hash",
            "least_connections",
        }

        if algo not in valid_algorithms:
            failures.append(f"{path} unsupported algorithm {algo}")

        if algo == "single" and len(upstreams) != 1:
            failures.append(f"{path} single algorithm must have exactly one upstream")

    return StabilityCheck(
        name="config_integrity",
        ok=not failures,
        details={
            "runtime": config.get("runtime") or config.get("runtime_mode"),
            "route_count": len(routes),
            "failures": failures,
        },
    )


def check_local() -> list[StabilityCheck]:
    checks: list[StabilityCheck] = [active_environment_check("local")]

    for name, url, expected in [
        ("local_homepage", "http://127.0.0.1:8090/", {200}),
        ("local_api", "http://127.0.0.1:8090/api/", {200}),
        ("local_blocked_env", "http://127.0.0.1:8090/.env", {403, 404}),
        ("local_runtime_state", "http://127.0.0.1:8090/__gateway_runtime_state", {200}),
    ]:
        status, body, headers = _http_status(url)
        checks.append(
            StabilityCheck(
                name=name,
                ok=status in expected,
                details={"url": url, "status": status, "body": body[:300], "headers": headers},
            )
        )

    checks.append(check_config_integrity(expected_runtime="local"))
    return checks


def check_docker() -> list[StabilityCheck]:
    checks: list[StabilityCheck] = [active_environment_check("docker")]

    api_headers = {"X-API-Key": "dev-local-secret"}

    for name, url, expected, headers in [
        ("docker_homepage", "https://127.0.0.1:8443/", {200}, None),
        ("docker_api", "https://127.0.0.1:8443/api/", {200}, api_headers),
        ("docker_api_rejects_missing_key", "https://127.0.0.1:8443/api/", {401, 403}, None),
        ("docker_blocked_env", "https://127.0.0.1:8443/.env", {401, 403, 404}, None),
        ("docker_edge_health", "http://127.0.0.1:8090/__edge_health", {200}, None),
    ]:
        status, body, resp_headers = _http_status(
            url,
            headers=headers,
            insecure_tls=url.startswith("https://"),
        )
        checks.append(
            StabilityCheck(
                name=name,
                ok=status in expected,
                details={"url": url, "status": status, "body": body[:300], "headers": resp_headers},
            )
        )

    checks.append(check_config_integrity(expected_runtime="docker_host"))
    return checks


def check_bare_metal(host: str) -> list[StabilityCheck]:
    checks: list[StabilityCheck] = [active_environment_check("linux")]

    for name, url, expected in [
        ("bare_homepage", f"http://{host}/", {200}),
        ("bare_api", f"http://{host}/api/", {200}),
        ("bare_blocked_env", f"http://{host}/.env", {403, 404}),
        ("bare_blocked_wp_admin", f"http://{host}/wp-admin", {403, 404}),
    ]:
        status, body, headers = _http_status(url)
        checks.append(
            StabilityCheck(
                name=name,
                ok=status in expected,
                details={"url": url, "status": status, "body": body[:300], "headers": headers},
            )
        )

    checks.append(
        StabilityCheck(
            name="config_integrity",
            ok=True,
            details={
                "skipped": True,
                "reason": "Bare-metal config lives on the remote host; local generated config may belong to local/docker.",
            },
        )
    )
    return checks


def sample_algorithm(
    url: str,
    *,
    expected_ports: set[str],
    attempts: int = 20,
    headers: dict[str, str] | None = None,
    insecure_tls: bool = False,
) -> StabilityCheck:
    counts: Counter[str] = Counter()

    for _ in range(attempts):
        status, body, response_headers = _http_status(
            url,
            headers=headers,
            insecure_tls=insecure_tls,
        )
        port = response_headers.get("X-Demo-Backend-Port")

        if status != 200:
            counts[f"HTTP_{status}"] += 1
        elif port:
            counts[str(port)] += 1
        else:
            counts["missing_backend_header"] += 1

    actual_ports = {p for p in counts if p and not p.startswith("HTTP_") and p != "missing_backend_header"}

    return StabilityCheck(
        name="algorithm_sampling",
        ok=actual_ports == expected_ports,
        details={
            "url": url,
            "expected_ports": sorted(expected_ports),
            "actual_ports": sorted(actual_ports),
            "counts": dict(counts),
        },
    )


def store_stability_run(result: dict[str, Any]) -> None:
    import sqlite3
    from datetime import datetime, UTC

    db_path = PROJECT_ROOT / "data" / "reliability_reports.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stability_guardian_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            environment TEXT,
            success INTEGER NOT NULL,
            result_json TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        INSERT INTO stability_guardian_runs (
            created_at, environment, success, result_json
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            datetime.now(UTC).isoformat(),
            result.get("environment"),
            1 if result.get("success") else 0,
            json.dumps(result, sort_keys=True, default=str),
        ),
    )

    conn.commit()
    conn.close()


def command_failure_to_agents(
    *,
    environment: str,
    operation: str,
    checks: list[StabilityCheck],
    config: dict[str, Any],
) -> dict[str, Any] | None:
    failed = [c for c in checks if not c.ok]
    if not failed:
        return None

    error = "; ".join(
        f"{item.name}: {item.details}"
        for item in failed[:8]
    )

    runtime = {
        "local": "local",
        "docker": "docker_host",
        "linux": "bare_metal_linux",
    }.get(environment, environment)

    strategy = "direct" if runtime == "local" else "bluegreen"

    try:
        from core.agent_incident_commander import record_runtime_failure_lesson

        return record_runtime_failure_lesson(
            runtime=runtime,
            strategy=strategy,
            operation=operation,
            error=error,
            config=config,
            allow_auto_repair=True,
        )
    except Exception as exc:
        return {
            "commander_error": str(exc),
            "error": error,
        }


def run_guardian(environment: str, host: str | None = None) -> dict[str, Any]:
    if environment == "local":
        checks = check_local()
    elif environment == "docker":
        checks = check_docker()
    elif environment == "linux":
        if not host:
            raise ValueError("--host is required for linux")
        checks = check_bare_metal(host)
    else:
        raise ValueError(f"Unsupported environment: {environment}")

    config = _load_config()
    success = all(check.ok for check in checks)

    incident = None
    if not success:
        failed = [check for check in checks if not check.ok]

        # If the requested environment is simply not active, record it
        # as a guardian state mismatch, not as a deploy/runtime repair incident.
        only_inactive_environment = (
            len(failed) >= 1
            and failed[0].name == "active_environment"
            and all(
                item.name in {
                    "active_environment",
                    "docker_homepage",
                    "docker_api",
                    "docker_api_rejects_missing_key",
                    "docker_blocked_env",
                    "docker_edge_health",
                    "local_homepage",
                    "local_api",
                    "local_blocked_env",
                    "local_runtime_state",
                    "config_integrity",
                }
                for item in failed
            )
        )

        if only_inactive_environment:
            incident = {
                "root_cause": "guardian_expected_environment_not_active",
                "category": "environment_selection",
                "repair_action": "deploy_expected_environment_before_guardian",
                "safe_to_auto_repair": False,
                "summary": f"Guardian was run for {environment}, but that environment is not active.",
                "hint": f"Deploy {environment} first, then run its guardian.",
            }
        else:
            incident = command_failure_to_agents(
                environment=environment,
                operation="stability_guardian",
                checks=checks,
                config=config,
            )

    result = {
        "environment": environment,
        "success": success,
        "checks": [
            {
                "name": check.name,
                "ok": check.ok,
                "details": check.details,
            }
            for check in checks
        ],
        "incident": incident,
    }

    store_stability_run(result)
    return result


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--environment", choices=["local", "docker", "linux"], required=True)
    parser.add_argument("--host", default=None)
    args = parser.parse_args()

    result = run_guardian(args.environment, host=args.host)

    print(json.dumps(result, indent=2, sort_keys=True, default=str))

    if not result["success"]:
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
