from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "generated-pingora-proxy" / "config.json"
BACKUP_DIR = PROJECT_ROOT / "data" / "update_backups"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise SystemExit("No generated config found. Run pingora-ai apply first.")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _save_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


def _backup_current(reason: str) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_DIR / f"manual-{reason}-{_utc_stamp()}"
    dest.mkdir(parents=True, exist_ok=True)

    if CONFIG_PATH.exists():
        shutil.copy2(CONFIG_PATH, dest / "config.json")

    public_index = PROJECT_ROOT / "generated-pingora-proxy" / "public" / "index.html"
    if public_index.exists():
        (dest / "public").mkdir(exist_ok=True)
        shutil.copy2(public_index, dest / "public" / "index.html")

    return dest


def _route_path(route: dict[str, Any]) -> str:
    path = str(route.get("path") or "/").strip()
    if not path.startswith("/"):
        path = "/" + path
    return path.rstrip("/") if len(path) > 1 else path


def _upstream_address(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("address") or item.get("upstream") or item.get("backend") or "").strip()
    return str(item or "").strip()


def _normalise_backend(value: str) -> str:
    text = str(value or "").strip()
    text = text.replace("localhost", "127.0.0.1")
    text = text.replace("http://", "").replace("https://", "").rstrip("/")
    return text


def _backend_candidates(value: str) -> set[str]:
    backend = _normalise_backend(value)
    candidates = {backend}

    if backend.startswith("127.0.0.1:"):
        candidates.add(backend.replace("127.0.0.1:", "host.docker.internal:", 1))
    if backend.startswith("host.docker.internal:"):
        candidates.add(backend.replace("host.docker.internal:", "127.0.0.1:", 1))

    return candidates


def _find_route(config: dict[str, Any], path: str) -> dict[str, Any]:
    target = _route_path({"path": path})
    for route in config.get("routes") or []:
        if isinstance(route, dict) and _route_path(route) == target:
            return route
    raise SystemExit(f"Route not found: {target}")


def _ensure_upstreams(route: dict[str, Any]) -> list[Any]:
    upstreams = route.get("upstreams")
    if not isinstance(upstreams, list):
        upstreams = []
        if route.get("upstream"):
            upstreams.append(route["upstream"])
        elif route.get("backend"):
            upstreams.append(route["backend"])
        route["upstreams"] = upstreams
    return upstreams


def _set_upstream_state(
    route: dict[str, Any],
    backend: str,
    *,
    state: str,
) -> bool:
    candidates = _backend_candidates(backend)
    upstreams = _ensure_upstreams(route)
    changed = False

    for i, item in enumerate(upstreams):
        address = _upstream_address(item)
        if address not in candidates:
            continue

        if isinstance(item, dict):
            before = dict(item)
            item["state"] = state
            item["enabled"] = state not in {"disabled", "draining"}
            item["draining"] = state == "draining"
            changed = changed or before != item
        else:
            upstreams[i] = {
                "address": address,
                "weight": 1,
                "state": state,
                "enabled": state not in {"disabled", "draining"},
                "draining": state == "draining",
            }
            changed = True

    if not changed:
        raise SystemExit(f"Backend not found on route {route.get('path')}: {backend}")

    return changed


def health_policy(path: str | None = None) -> int:
    config = _load_config()
    routes = config.get("routes") or []

    print("")
    print("🩺 Health-check policy")
    print("")

    for route in routes:
        if not isinstance(route, dict):
            continue

        route_path = _route_path(route)
        if path and route_path != _route_path({"path": path}):
            continue

        if route_path == "/":
            continue

        policy = route.get("health_check") or {}
        print(f"Route: {route_path}")
        print(f"  enabled: {policy.get('enabled', False)}")
        print(f"  path: {policy.get('path', '/health')}")
        print(f"  interval_seconds: {policy.get('interval_seconds', 5)}")
        print(f"  timeout_seconds: {policy.get('timeout_seconds', 2)}")
        print(f"  healthy_threshold: {policy.get('healthy_threshold', 2)}")
        print(f"  unhealthy_threshold: {policy.get('unhealthy_threshold', 3)}")
        print("")

    return 0


def upstream_state_change(
    *,
    action: str,
    path: str,
    backend: str,
    confirm: bool,
    dry_run: bool,
) -> int:
    if not confirm:
        print("❌ This changes upstream state. Re-run with --confirm-route-mutation.")
        return 2

    config = _load_config()
    route = _find_route(config, path)

    state_map = {
        "drain": "draining",
        "disable": "disabled",
        "enable": "enabled",
    }

    state = state_map[action]

    if dry_run:
        print("")
        print("🧪 Dry run only. No config changed.")
        print(f"Would set {backend} on {path} to state={state}")
        return 0

    backup = _backup_current(reason=f"before-upstream-{action}")
    _set_upstream_state(route, backend, state=state)

    # Store an audit trail in config for now. Later move to SQLite.
    config.setdefault("_mutation_audit", []).append(
        {
            "time": datetime.now(timezone.utc).isoformat(),
            "action": f"upstream_{action}",
            "route": path,
            "backend": backend,
            "state": state,
            "backup": str(backup),
        }
    )

    _save_config(config)

    print("")
    print(f"✅ Upstream {action} applied in config.")
    print(f"Route: {path}")
    print(f"Backend: {backend}")
    print(f"State: {state}")
    print(f"Backup: {backup}")
    print("")
    print("Next step: run deploy/update verification to apply this config to runtime.")
    return 0


def backup_restore(*, backup_name: str, confirm: bool) -> int:
    if not confirm:
        print("❌ Restore changes the active generated config. Re-run with --confirm.")
        return 2

    backup_path = BACKUP_DIR / backup_name
    config_backup = backup_path / "config.json"

    if not config_backup.exists():
        raise SystemExit(f"Backup config not found: {config_backup}")

    safety = _backup_current(reason="before-backup-restore")
    shutil.copy2(config_backup, CONFIG_PATH)

    public_backup = backup_path / "public" / "index.html"
    public_target = PROJECT_ROOT / "generated-pingora-proxy" / "public" / "index.html"
    if public_backup.exists():
        public_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(public_backup, public_target)

    print("")
    print("✅ Backup restored into generated project files.")
    print(f"Restored from: {backup_path}")
    print(f"Safety backup before restore: {safety}")
    print("")
    print("Next step: run pingora-ai apply/update for your target runtime if you want this live.")
    return 0
