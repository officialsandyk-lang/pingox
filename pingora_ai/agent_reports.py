from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from statistics import mean
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "data" / "agent_reports.sqlite3"
CONFIG_PATH = PROJECT_ROOT / "generated-pingora-proxy" / "config.json"

DOCKER_PUBLIC_BASE = "http://127.0.0.1"
CONTROL_API_BASE = "http://127.0.0.1:8787"



def _base_for_context(runtime_context: str = "config_target", address_mode: str = "config_target") -> tuple[str, str]:
    ctx = str(runtime_context or "config_target").lower()
    mode = str(address_mode or "config_target").lower()

    if ctx == "local" or mode == "local":
        return "http://127.0.0.1:8080", "local"

    if ctx in {"linux", "linux_systemd"} or mode in {"linux", "linux_systemd"}:
        return "http://127.0.0.1:80", "linux_systemd"

    return DOCKER_PUBLIC_BASE, "docker_host"

def _now_ms() -> int:
    return int(time.time() * 1000)


def _run(command: list[str], timeout: int = 60) -> dict[str, Any]:
    started = time.perf_counter()

    try:
        result = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            text=True,
            capture_output=True,
            timeout=timeout,
        )

        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "command": command,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "stdout_tail": result.stdout[-4000:],
            "stderr_tail": result.stderr[-4000:],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": None,
            "command": command,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "error": f"timeout after {timeout}s",
            "stdout_tail": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "",
            "stderr_tail": (exc.stderr or "")[-4000:] if isinstance(exc.stderr, str) else "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "returncode": None,
            "command": command,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "error": str(exc),
        }


def _http_get(url: str, headers: dict[str, str] | None = None, timeout: float = 5.0) -> dict[str, Any]:
    started = time.perf_counter()

    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return {
                "ok": 200 <= response.status < 400,
                "status": response.status,
                "url": url,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "headers": dict(response.headers.items()),
                "body": body,
                "body_tail": body[-4000:],
            }
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {
            "ok": False,
            "status": exc.code,
            "url": url,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "error": body[-1000:],
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": None,
            "url": url,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "error": str(exc),
        }


def _load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}

    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _extract_backend_port(response: dict[str, Any]) -> str | None:
    headers = response.get("headers") or {}

    for key, value in headers.items():
        if key.lower() == "x-demo-backend-port":
            return str(value)

    body = str(response.get("body_tail") or "")
    match = re.search(r'"backend_port"\s*:\s*(\d+)', body)
    if match:
        return match.group(1)

    match = re.search(r"Demo Backend\s+(\d+)", body)
    if match:
        return match.group(1)

    return None


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None

    ordered = sorted(values)
    index = int(round((len(ordered) - 1) * pct))
    return round(ordered[index], 2)


def _init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_unix_ms INTEGER NOT NULL,
                agent TEXT NOT NULL,
                ok INTEGER NOT NULL,
                report_json TEXT NOT NULL
            )
            """
        )
        conn.commit()


def store_agent_report(agent: str, report: dict[str, Any]) -> dict[str, Any]:
    _init_db()

    ok = bool(report.get("ok"))

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """
            INSERT INTO agent_reports (created_unix_ms, agent, ok, report_json)
            VALUES (?, ?, ?, ?)
            """,
            (_now_ms(), agent, 1 if ok else 0, json.dumps(report, indent=2, default=str)),
        )
        conn.commit()
        report_id = cur.lastrowid

    return {
        "stored": True,
        "db_path": str(DB_PATH),
        "report_id": report_id,
    }


# ---------------------------------------------------------------------
# 1. Upstream State Agent
# ---------------------------------------------------------------------

def upstream_state_report() -> dict[str, Any]:
    config = _load_config()
    routes = []

    for route in config.get("routes") or []:
        if not isinstance(route, dict):
            continue

        path = str(route.get("path") or "/")
        route_type = str(route.get("type") or route.get("route_type") or "proxy")
        balancing = str(route.get("balancing") or route.get("algorithm") or "single")

        upstream_items = []
        raw_upstreams = route.get("upstreams") or []

        if isinstance(raw_upstreams, list):
            for item in raw_upstreams:
                if isinstance(item, dict):
                    address = item.get("address") or item.get("upstream") or item.get("backend")
                    upstream_items.append(
                        {
                            "address": str(address or ""),
                            "weight": item.get("weight", 1),
                            "state": item.get("state", "enabled"),
                            "enabled": item.get("enabled", True),
                            "draining": item.get("draining", False),
                            "role": item.get("role", "primary"),
                            "backup": item.get("backup", False),
                        }
                    )
                else:
                    upstream_items.append(
                        {
                            "address": str(item),
                            "weight": 1,
                            "state": "enabled",
                            "enabled": True,
                            "draining": False,
                            "role": "primary",
                            "backup": False,
                        }
                    )

        routes.append(
            {
                "path": path,
                "type": route_type,
                "balancing": balancing,
                "upstreams": upstream_items,
            }
        )

    runtime_state = _http_get(f"{DOCKER_PUBLIC_BASE}/__gateway_runtime_state")

    report = {
        "ok": True,
        "agent": "upstream_state_agent",
        "mode": "read_only",
        "config_path": str(CONFIG_PATH),
        "routes": routes,
        "runtime_state_status": runtime_state.get("status"),
        "runtime_state_ok": runtime_state.get("ok"),
    }

    report["lesson"] = store_agent_report("upstream_state_agent", report)
    return report


# ---------------------------------------------------------------------
# 2. Runtime Admin Agent
# ---------------------------------------------------------------------

def runtime_admin_report(runtime_context: str = "config_target", address_mode: str = "config_target") -> dict[str, Any]:
    base_url = DOCKER_PUBLIC_BASE

    if str(runtime_context).lower() == "local" or str(address_mode).lower() == "local":
        base_url = "http://127.0.0.1:8080"

    root = _http_get(f"{base_url}/")
    api = _http_get(f"{base_url}/api/")
    runtime_state = _http_get(f"{base_url}/__gateway_runtime_state")

    report = {
        "ok": bool(root.get("ok") and api.get("ok") and runtime_state.get("ok")),
        "agent": "runtime_admin_agent",
        "mode": "read_only",
        "runtime_context": runtime_context,
        "address_mode": address_mode,
        "checked_base_url": base_url,
        "checks": {
            "root": {"status": root.get("status"), "latency_ms": root.get("latency_ms")},
            "api": {"status": api.get("status"), "latency_ms": api.get("latency_ms")},
            "runtime_state": {
                "status": runtime_state.get("status"),
                "latency_ms": runtime_state.get("latency_ms"),
            },
        },
        "note": "Runtime Admin Agent is read-only in this phase. No live mutation endpoint is called.",
    }

    report["lesson"] = store_agent_report("runtime_admin_agent", report)
    return report


# ---------------------------------------------------------------------
# 3. Decision Integrity Agent
# ---------------------------------------------------------------------

def decision_integrity_report() -> dict[str, Any]:
    checks = []

    for file_path in [
        "core/project_writer.py",
        "pingora_ai/control_api.py",
        "pingora_ai/agent_reports.py",
    ]:
        checks.append(
            {
                "name": f"py_compile {file_path}",
                "result": _run(["python", "-m", "py_compile", file_path], timeout=30),
            }
        )

    checks.append(
        {
            "name": "cargo check generated runtime",
            "result": _run(
                ["cargo", "check", "--manifest-path", "generated-pingora-proxy/Cargo.toml"],
                timeout=120,
            ),
        }
    )

    mutation_policy = {
        "invalid_mutations_blocked_before_backup_build_deploy": True,
        "live_docker_linux_tests_require_explicit_flags": True,
        "mutation_buttons_execute": False,
        "mutation_buttons_mode": "disabled_or_dry_run_only",
        "decision_integrity_before_backup_build_deploy": True,
    }

    ok = all(item["result"].get("ok") for item in checks)

    report = {
        "ok": ok,
        "agent": "decision_integrity_agent",
        "mode": "read_only",
        "checks": checks,
        "mutation_policy": mutation_policy,
    }

    report["lesson"] = store_agent_report("decision_integrity_agent", report)
    return report


# ---------------------------------------------------------------------
# 4. Performance Agent
# ---------------------------------------------------------------------

def performance_report(
    requests: int = 30,
    runtime_context: str = "config_target",
    address_mode: str = "config_target",
) -> dict[str, Any]:
    requests = max(1, min(int(requests or 30), 200))

    base_url = DOCKER_PUBLIC_BASE
    environment = "docker_host"

    if str(runtime_context).lower() == "local" or str(address_mode).lower() == "local":
        base_url = "http://127.0.0.1:8080"
        environment = "local"

    root = _http_get(f"{base_url}/")
    runtime_state = _http_get(f"{base_url}/__gateway_runtime_state")

    api_results = []
    backend_distribution: dict[str, int] = {}

    for _ in range(requests):
        item = _http_get(f"{base_url}/api/", timeout=5)
        api_results.append(item)

        port = _extract_backend_port(item)
        if port:
            backend_distribution[port] = backend_distribution.get(port, 0) + 1

    latencies = [float(item["latency_ms"]) for item in api_results if item.get("latency_ms") is not None]
    success_count = sum(1 for item in api_results if item.get("ok"))
    statuses: dict[str, int] = {}

    for item in api_results:
        key = str(item.get("status"))
        statuses[key] = statuses.get(key, 0) + 1

    report = {
        "ok": bool(root.get("ok") and runtime_state.get("ok") and success_count == requests),
        "agent": "performance_agent",
        "mode": "read_only_get_requests",
        "environment": environment,
        "runtime_context": runtime_context,
        "address_mode": address_mode,
        "checked_base_url": base_url,
        "requests": requests,
        "root_status": root.get("status"),
        "runtime_state_status": runtime_state.get("status"),
        "success_count": success_count,
        "success_rate": round((success_count / requests) * 100, 2),
        "avg_latency_ms": round(mean(latencies), 2) if latencies else None,
        "p95_latency_ms": _percentile(latencies, 0.95),
        "p99_latency_ms": _percentile(latencies, 0.99),
        "statuses": statuses,
        "backend_distribution": backend_distribution,
    }

    report["lesson"] = store_agent_report("performance_agent", report)
    return report


# ---------------------------------------------------------------------
# 5. Lesson Learning Agent
# ---------------------------------------------------------------------

def lesson_learning_report(limit: int = 10) -> dict[str, Any]:
    _init_db()

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT id, created_unix_ms, agent, ok
            FROM agent_reports
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, min(int(limit or 10), 50)),),
        ).fetchall()

    return {
        "ok": True,
        "agent": "lesson_learning_agent",
        "mode": "read_only",
        "db_path": str(DB_PATH),
        "recent_reports": [
            {
                "id": row[0],
                "created_unix_ms": row[1],
                "agent": row[2],
                "ok": bool(row[3]),
            }
            for row in rows
        ],
    }


# ---------------------------------------------------------------------
# 6. Dashboard Report Agent
# ---------------------------------------------------------------------

def dashboard_report(runtime_context: str = "config_target", address_mode: str = "config_target") -> dict[str, Any]:
    base_url, environment = _base_for_context(runtime_context, address_mode)
    dashboard = _http_get(f"{base_url}/")
    body = str(dashboard.get("body") or dashboard.get("body_tail") or "")

    required_markers = [
        "AI Pingora Gateway Dashboard",
        "Gateway Operations",
        "controlApiUrl",
        "controlApiToken",
        "runtimeStateButton",
        "Fetch Runtime State",
        "/__gateway_runtime_state",
        "Live Disable Upstream",
        "Live Drain Upstream",
        "Live Enable Upstream",
        "Drain Upstream",
        "Disable Upstream",
        "Enable Upstream",
    ]

    marker_results = {
        marker: marker in body
        for marker in required_markers
    }

    control_health = _http_get(f"{CONTROL_API_BASE}/health")
    upstream_endpoint = _http_get(
        f"{CONTROL_API_BASE}/__gateway_control/upstreams",
        headers={"X-Pingora-Control-Token": "dev-local-control-token"},
    )

    ok = bool(
        dashboard.get("ok")
        and all(marker_results.values())
        and control_health.get("ok")
        and upstream_endpoint.get("ok")
    )

    report = {
        "ok": ok,
        "agent": "dashboard_report_agent",
        "mode": "read_only",
        "runtime_context": runtime_context,
        "address_mode": address_mode,
        "environment": environment,
        "checked_base_url": base_url,
        "dashboard_status": dashboard.get("status"),
        "dashboard_markers": marker_results,
        "control_api_health_status": control_health.get("status"),
        "upstream_endpoint_status": upstream_endpoint.get("status"),
        "mutation_buttons_should_execute": False,
    }

    report["lesson"] = store_agent_report("dashboard_report_agent", report)
    return report


def full_agent_report() -> dict[str, Any]:
    reports = {
        "runtime_admin": runtime_admin_report(),
        "upstream_state": upstream_state_report(),
        "decision_integrity": decision_integrity_report(),
        "performance": performance_report(requests=20),
        "dashboard": dashboard_report(),
        "lessons": lesson_learning_report(limit=10),
    }

    return {
        "ok": all(bool(item.get("ok")) for item in reports.values() if isinstance(item, dict)),
        "agent": "dashboard_report_agent",
        "mode": "read_only_full_report",
        "reports": reports,
    }

# ---------------------------------------------------------------------
# Upstream State Agent: dry-run mutation decisions only
# ---------------------------------------------------------------------

def _normalise_upstream_address(value: str) -> str:
    text = str(value or "").strip()
    text = text.replace("http://", "").replace("https://", "").rstrip("/")
    text = text.replace("localhost:", "127.0.0.1:")
    return text




def _apply_address_mode(value: str, address_mode: str | None = None) -> str:
    text = _normalise_upstream_address(value)
    mode = str(address_mode or "").strip().lower()

    if mode in {"local", "local_manual", "direct"}:
        return text.replace("host.docker.internal:", "127.0.0.1:")

    if mode in {"docker", "docker_host", "bluegreen"}:
        return text.replace("127.0.0.1:", "host.docker.internal:")

    return text

def _upstream_candidates(value: str) -> set[str]:
    value = _normalise_upstream_address(value)
    candidates = {value}

    if value.startswith("127.0.0.1:"):
        candidates.add(value.replace("127.0.0.1:", "host.docker.internal:", 1))

    if value.startswith("host.docker.internal:"):
        candidates.add(value.replace("host.docker.internal:", "127.0.0.1:", 1))

    return candidates



def _load_live_local_runtime_route(route_path: str) -> dict[str, Any] | None:
    """
    Local-only source of truth for dry-run upstream decisions.

    This prevents local dry-run buttons from reading stale config files such as
    data/api_plans/latest_config.json after a Docker/apply command.
    It does not mutate runtime, config, Docker, Linux, or files.
    """
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:8080/__gateway_runtime_state",
            timeout=3,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None

    for route in payload.get("routes") or []:
        if not isinstance(route, dict):
            continue

        if str(route.get("path") or "") != route_path:
            continue

        upstreams = []
        for item in route.get("upstreams") or []:
            if not isinstance(item, dict):
                continue

            address = item.get("address") or item.get("upstream") or item.get("backend") or ""
            if not address:
                continue

            state = str(
                item.get("state")
                or item.get("effective_state")
                or item.get("config_state")
                or "enabled"
            ).lower()

            role = str(item.get("role") or ("backup" if item.get("backup") else "primary")).lower()

            enabled = item.get("enabled")
            if enabled is None:
                enabled = state not in {"disabled", "draining", "drain"}

            draining = bool(item.get("draining", state in {"draining", "drain"}))

            upstreams.append(
                {
                    "address": str(address).replace("http://", "").replace("https://", "").rstrip("/"),
                    "weight": item.get("weight", 1),
                    "state": state,
                    "enabled": bool(enabled),
                    "draining": draining,
                    "role": role,
                    "backup": bool(item.get("backup", role in {"backup", "standby", "secondary"})),
                }
            )

        return {
            "path": route_path,
            "upstreams": upstreams,
            "_source": "live_local_runtime_state",
        }

    return None


def upstream_mutation_dry_run(
    *,
    action: str,
    route_path: str = "/api",
    upstream: str = "",
    weight: int | None = None,
    runtime_context: str | None = None,
    address_mode: str | None = None,
) -> dict[str, Any]:
    """
    Dry-run only. Does not mutate runtime, Docker, Linux, config, backup, or deploy.
    """

    config = _load_config()
    route_path = str(route_path or "/api").strip()
    if not route_path.startswith("/"):
        route_path = "/" + route_path

    action = str(action or "").strip().lower()
    upstream = _apply_address_mode(upstream, address_mode)

    allowed_actions = {
        "disable",
        "drain",
        "enable",
        "weight",
        "promote_backup",
        "promote-backup",
    }

    if action not in allowed_actions:
        report = {
            "ok": False,
            "agent": "upstream_state_agent",
            "mode": "dry_run_only",
            "decision": "blocked",
            "reason": f"Unknown upstream action: {action}",
            "will_not_execute": True,
        }
        report["lesson"] = store_agent_report("upstream_state_agent", report)
        return report

    matched_route = None
    matched_route_source = "config"

    if str(runtime_context or "").lower() == "local" and str(address_mode or "").lower() == "local":
        matched_route = _load_live_local_runtime_route(route_path)
        if matched_route:
            matched_route_source = "live_local_runtime_state"

    if matched_route is None:
        for route in config.get("routes") or []:
            if isinstance(route, dict) and str(route.get("path") or "") == route_path:
                matched_route = route
                break

    if not matched_route:
        report = {
            "ok": False,
            "agent": "upstream_state_agent",
            "mode": "dry_run_only",
            "action": action,
            "route": route_path,
            "upstream": upstream,
            "decision": "blocked",
            "reason": f"Route does not exist: {route_path}",
            "would_change": False,
            "requires_confirmation": False,
            "risk": "none",
            "will_not_execute": True,
        }
        report["lesson"] = store_agent_report("upstream_state_agent", report)
        return report

    raw_upstreams = matched_route.get("upstreams") or []
    normalised = []

    for item in raw_upstreams:
        if isinstance(item, dict):
            address = item.get("address") or item.get("upstream") or item.get("backend") or ""
            state = str(item.get("state") or "enabled").lower()
            role = str(item.get("role") or ("backup" if item.get("backup") else "primary")).lower()
            enabled = item.get("enabled")
            if enabled is None:
                enabled = state not in {"disabled", "draining", "drain"}

            normalised.append(
                {
                    "address": _apply_address_mode(address, address_mode),
                    "weight": item.get("weight", 1),
                    "state": state,
                    "enabled": bool(enabled),
                    "draining": bool(item.get("draining", state in {"draining", "drain"})),
                    "role": role,
                    "backup": bool(item.get("backup", role in {"backup", "standby", "secondary"})),
                }
            )
        else:
            normalised.append(
                {
                    "address": _apply_address_mode(str(item), address_mode),
                    "weight": 1,
                    "state": "enabled",
                    "enabled": True,
                    "draining": False,
                    "role": "primary",
                    "backup": False,
                }
            )

    target = None
    candidates = _upstream_candidates(upstream)

    for item in normalised:
        if item["address"] in candidates:
            target = item
            break

    if not target:
        report = {
            "ok": False,
            "agent": "upstream_state_agent",
            "mode": "dry_run_only",
            "action": action,
            "route": route_path,
            "upstream": upstream,
            "decision": "blocked",
            "reason": f"Upstream does not exist on route {route_path}: {upstream}",
            "known_upstreams": [item["address"] for item in normalised],
            "would_change": False,
            "requires_confirmation": False,
            "risk": "none",
            "will_not_execute": True,
        }
        report["lesson"] = store_agent_report("upstream_state_agent", report)
        return report

    active_primary = [
        item for item in normalised
        if item["role"] == "primary"
        and not item["backup"]
        and item["enabled"]
        and not item["draining"]
        and item["state"] not in {"disabled", "draining", "drain"}
    ]

    risk = "medium"
    decision = "allowed"
    reason = "Dry-run allowed. No runtime change will be executed."

    if action in {"disable", "drain"}:
        target_is_active_primary = (
            target["role"] == "primary"
            and not target["backup"]
            and target["enabled"]
            and not target["draining"]
            and target["state"] not in {"disabled", "draining", "drain"}
        )

        remaining_active_primary = len(active_primary) - (1 if target_is_active_primary else 0)

        if remaining_active_primary < 1:
            decision = "blocked"
            risk = "high"
            reason = "Cannot disable/drain the last active primary upstream. Add/enable another primary or promote a backup first."

    if action == "weight":
        try:
            weight_value = int(weight or 0)
        except Exception:
            weight_value = 0

        if weight_value < 1:
            decision = "blocked"
            risk = "low"
            reason = "Weight must be >= 1."

    would_after = dict(target)

    if decision == "allowed":
        if action == "disable":
            would_after.update({"state": "disabled", "enabled": False, "draining": False})
        elif action == "drain":
            would_after.update({"state": "draining", "enabled": False, "draining": True})
        elif action == "enable":
            would_after.update({"state": "enabled", "enabled": True, "draining": False})
        elif action == "weight":
            would_after.update({"weight": int(weight or target.get("weight") or 1)})
        elif action in {"promote_backup", "promote-backup"}:
            would_after.update({"role": "primary", "backup": False, "state": "enabled", "enabled": True, "draining": False})

    report = {
        "ok": decision == "allowed",
        "agent": "upstream_state_agent",
        "mode": "dry_run_only",
        "runtime_context": runtime_context or "config_target",
        "address_mode": address_mode or "config_target",
        "action": action,
        "route": route_path,
        "upstream": target["address"],
        "source": matched_route_source,
        "decision": decision,
        "reason": reason,
        "risk": risk,
        "would_change": decision == "allowed",
        "requires_confirmation": decision == "allowed",
        "will_not_execute": True,
        "before": target,
        "after_preview": would_after,
        "active_primary_count": len(active_primary),
        "all_upstreams": normalised,
        "safety": {
            "docker_apply": False,
            "linux_deploy": False,
            "restart": False,
            "rollback": False,
            "live_runtime_mutation": False,
        },
    }

    report["lesson"] = store_agent_report("upstream_state_agent", report)
    return report
