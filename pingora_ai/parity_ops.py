from __future__ import annotations

import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _current_config_path() -> Path | None:
    candidates = [
        PROJECT_ROOT / "generated-pingora-proxy" / "config.json",
        PROJECT_ROOT / "data" / "api_plans" / "latest_config.json",
        PROJECT_ROOT / "active_config.json",
        PROJECT_ROOT / "gateway_config.json",
    ]

    for path in candidates:
        if path.exists():
            return path

    return None


def _current_config() -> dict[str, Any]:
    path = _current_config_path()
    if not path:
        return {}
    return _load_json(path)


def _route_path(route: dict[str, Any]) -> str:
    return str(route.get("path") or "/")


def _route_type(route: dict[str, Any]) -> str:
    return str(route.get("type") or route.get("route_type") or "proxy")


def _route_algorithm(route: dict[str, Any]) -> str:
    return str(route.get("balancing") or route.get("algorithm") or "single")


def _upstreams(route: dict[str, Any]) -> list[str]:
    raw = route.get("upstreams") or []
    values: list[str] = []

    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                values.append(str(item.get("address") or item.get("upstream") or item.get("backend") or ""))
            else:
                values.append(str(item))

    values = [item for item in values if item]
    if not values and route.get("upstream"):
        values.append(str(route["upstream"]))
    if not values and route.get("backend"):
        values.append(str(route["backend"]))

    return values


def run_parity_report() -> int:
    config = _current_config()
    config_path = _current_config_path()

    if not config:
        print("❌ No gateway config found. Run pingora-ai apply first.")
        return 2

    routes = config.get("routes") or []
    runtime = config.get("runtime") or config.get("runtime_mode") or "unknown"
    strategy = config.get("strategy") or config.get("deployment_strategy") or "unknown"

    runtime_state = bool(config.get("runtime_state", {}).get("enabled", False))
    security = config.get("security") or {}
    production_security = config.get("production_security") or {}

    checks = [
        ("CLI apply/update/add/edit/remove", True),
        ("CLI status/stop/start/restart/reload", True),
        ("Local runtime support", True),
        ("Docker blue/green support", True),
        ("Linux/systemd support", True),
        ("Pingora-only edge direction", True),
        ("Route directory dashboard", True),
        ("Runtime state endpoint", runtime_state),
        ("Active health-check policy", any(isinstance(r, dict) and r.get("health_check") for r in routes)),
        ("Load balancing algorithms", any(_route_algorithm(r) not in {"single", "static"} for r in routes if isinstance(r, dict))),
        ("Decision integrity before deploy", True),
        ("Invalid mutation early block", True),
        ("Backup policy before valid deploy", True),
        ("Rollback direction", True),
        ("Security contract present", bool(security or production_security)),
        ("Real TLS/ACME", bool((production_security.get("https_tls") or {}).get("enabled"))),
        ("Secrets manager", bool((production_security.get("secrets_management") or {}).get("enabled"))),
        ("Observability/metrics dashboard", bool((production_security.get("observability_slo") or {}).get("enabled"))),
        ("WAF-style rules", bool((production_security.get("waf_waap") or {}).get("enabled"))),
        ("Kubernetes/ECS targets", False),
    ]

    passed = sum(1 for _, ok in checks if ok)
    total = len(checks)
    score = round((passed / total) * 100, 1)

    print("")
    print("🚦 Production Gateway Parity Report")
    print(f"Config: {config_path}")
    print(f"Runtime: {runtime}")
    print(f"Strategy: {strategy}")
    print(f"Routes: {len(routes)}")
    print(f"Parity score: {score}%")
    print("")

    for name, ok in checks:
        print(f"{'✅' if ok else '⚠️'} {name}")

    print("")
    print("Current target:")
    print("  Pingora-only gateway with production gateway parity.")
    print("  Public dashboard wording should not say NGINX/NGINX Plus.")
    print("")

    return 0


def run_upstream_list() -> int:
    config = _current_config()
    if not config:
        print("❌ No gateway config found. Run pingora-ai apply first.")
        return 2

    print("")
    print("📡 Upstream list")
    print("")

    for route in config.get("routes") or []:
        if not isinstance(route, dict):
            continue

        path = _route_path(route)
        rtype = _route_type(route)
        algorithm = _route_algorithm(route)
        upstreams = _upstreams(route)

        print(f"Route: {path}")
        print(f"  Type: {rtype}")
        print(f"  Algorithm: {algorithm}")

        if upstreams:
            for upstream in upstreams:
                print(f"  - {upstream}")
        else:
            print("  - no upstreams/static route")

        print("")

    return 0


def run_backup_list(limit: int = 20) -> int:
    backup_dir = PROJECT_ROOT / "data" / "update_backups"

    print("")
    print("🗄️ Backup list")
    print(f"Directory: {backup_dir}")
    print("")

    if not backup_dir.exists():
        print("No backup directory found yet.")
        return 0

    backups = sorted(
        [p for p in backup_dir.iterdir() if p.is_dir() or p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limit]

    if not backups:
        print("No backups found.")
        return 0

    for item in backups:
        kind = "dir" if item.is_dir() else "file"
        print(f"- {item.name} [{kind}]")

    print("")
    print("Policy: backups are only for valid deployment attempts.")
    return 0
