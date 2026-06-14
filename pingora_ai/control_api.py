from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "generated-pingora-proxy" / "config.json"
BACKUP_DIR = PROJECT_ROOT / "data" / "update_backups"


CONTROL_API_TOKEN_ENV = "PINGORA_AI_CONTROL_TOKEN"
DEFAULT_DEV_TOKEN = "dev-local-control-token"


app = FastAPI(
    title="Pingora AI Gateway Control API",
    version="0.1.0",
    description="Authenticated local control API for dashboard operations.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1",
        "http://127.0.0.1:80",
        "http://127.0.0.1:8080",
        "http://localhost",
        "http://localhost:80",
        "http://localhost:8080",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["X-Pingora-Control-Token", "Content-Type"],
)


def _expected_token() -> str:
    return os.getenv(CONTROL_API_TOKEN_ENV, DEFAULT_DEV_TOKEN).strip()


def _require_token(x_pingora_control_token: str | None) -> None:
    expected = _expected_token()

    if not expected:
        raise HTTPException(status_code=500, detail="Control API token is not configured.")

    if not x_pingora_control_token or x_pingora_control_token != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing control token.")


def _run(command: list[str], timeout: int = 60) -> dict[str, Any]:
    result = subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        text=True,
        capture_output=True,
        timeout=timeout,
    )

    return {
        "returncode": result.returncode,
        "stdout": result.stdout[-8000:],
        "stderr": result.stderr[-8000:],
        "ok": result.returncode == 0,
        "command": command,
    }



ACTIVE_RUNTIME_CONTEXT_PATH = PROJECT_ROOT / "data" / "active_runtime_context.json"


def _read_active_runtime_context() -> dict[str, Any]:
    try:
        if ACTIVE_RUNTIME_CONTEXT_PATH.exists():
            data = json.loads(ACTIVE_RUNTIME_CONTEXT_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _resolve_runtime_context(
    runtime_context: str = "config_target",
    address_mode: str = "config_target",
) -> tuple[str, str, str]:
    ctx = str(runtime_context or "config_target")
    mode = str(address_mode or "config_target")

    if ctx != "config_target" or mode != "config_target":
        base_url = "http://127.0.0.1:8080" if (ctx == "local" or mode == "local") else "http://127.0.0.1:80"
        return ctx, mode, base_url

    active = _read_active_runtime_context()
    if active:
        resolved_ctx = str(active.get("runtime_context") or "config_target")
        resolved_mode = str(active.get("address_mode") or "config_target")
        resolved_base = str(active.get("base_url") or "http://127.0.0.1:80")
        return resolved_ctx, resolved_mode, resolved_base

    return ctx, mode, "http://127.0.0.1:80"


def _active_config_path() -> Path:
    active = _read_active_runtime_context()
    active_path = active.get("active_config_path")
    if active_path:
        candidate = Path(str(active_path))
        if candidate.exists():
            return candidate

    return CONFIG_PATH


def _load_active_config() -> dict[str, Any]:
    path = _active_config_path()
    if not path.exists():
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}

    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _route_path(route: dict[str, Any]) -> str:
    path = str(route.get("path") or "/").strip()
    if not path.startswith("/"):
        path = "/" + path
    return path.rstrip("/") if len(path) > 1 else path


def _route_type(route: dict[str, Any]) -> str:
    return str(route.get("type") or route.get("route_type") or "proxy")


def _route_algorithm(route: dict[str, Any]) -> str:
    return str(route.get("balancing") or route.get("algorithm") or "single")


def _upstream_address(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("address") or item.get("upstream") or item.get("backend") or "").strip()
    return str(item or "").strip()



def _display_address_for_mode(address: str, address_mode: str | None = None) -> str:
    value = str(address or "").strip()
    mode = str(address_mode or "").strip().lower()

    if mode in {"local", "local_manual", "direct"}:
        return value.replace("host.docker.internal:", "127.0.0.1:")

    if mode in {"docker", "docker_host", "bluegreen"}:
        return value.replace("127.0.0.1:", "host.docker.internal:")

    return value

def _upstreams(route: dict[str, Any]) -> list[dict[str, Any]]:
    raw = route.get("upstreams") or []
    values: list[dict[str, Any]] = []

    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                values.append(
                    {
                        "address": _upstream_address(item),
                        "weight": item.get("weight", 1),
                        "state": item.get("state", "enabled"),
                        "enabled": item.get("enabled", True),
                        "draining": item.get("draining", False),
                    }
                )
            else:
                values.append(
                    {
                        "address": str(item),
                        "weight": 1,
                        "state": "enabled",
                        "enabled": True,
                        "draining": False,
                    }
                )

    if not values and route.get("upstream"):
        values.append(
            {
                "address": str(route["upstream"]),
                "weight": 1,
                "state": "enabled",
                "enabled": True,
                "draining": False,
            }
        )

    if not values and route.get("backend"):
        values.append(
            {
                "address": str(route["backend"]),
                "weight": 1,
                "state": "enabled",
                "enabled": True,
                "draining": False,
            }
        )

    return [item for item in values if item.get("address")]


def _backup_items(limit: int = 20) -> list[dict[str, Any]]:
    if not BACKUP_DIR.exists():
        return []

    items = sorted(
        [p for p in BACKUP_DIR.iterdir()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limit]

    return [
        {
            "name": item.name,
            "path": str(item),
            "type": "directory" if item.is_dir() else "file",
            "modified_unix": item.stat().st_mtime,
        }
        for item in items
    ]


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "pingora-ai-control-api",
        "mode": "read_only_phase_1",
    }


@app.get("/__gateway_control/status")
def control_status(
    x_pingora_control_token: str | None = Header(default=None),
    runtime_context: str = "config_target",
    address_mode: str = "config_target",
) -> dict[str, Any]:
    _require_token(x_pingora_control_token)

    runtime_context, address_mode, base_url = _resolve_runtime_context(runtime_context, address_mode)

    config = _load_active_config()
    routes = config.get("routes") or []

    docker_status_raw = _run(
        [
            "bash",
            "-lc",
            "docker ps -a --filter 'name=ai-pingora-gateway' "
            "--format 'table {{.Names}}\\t{{.Status}}\\t{{.Ports}}' 2>/dev/null || true",
        ],
        timeout=30,
    )

    runtime_is_local = (
        str(runtime_context).lower() == "local"
        or str(address_mode).lower() == "local"
    )

    docker_lines = [
        line for line in (docker_status_raw.get("stdout") or "").splitlines()
        if line.strip()
    ]
    docker_active = len(docker_lines) > 1
    docker_expected = not runtime_is_local

    docker_status = {
        **docker_status_raw,
        "command_ok": docker_status_raw.get("returncode") == 0,
        "active": docker_active,
        "expected": docker_expected,
        "ok": True if not docker_expected else docker_active,
        "reason": (
            "docker containers not required for local runtime"
            if not docker_expected
            else "docker runtime expected"
        ),
    }

    # base_url resolved by _resolve_runtime_context()

    public_checks = {}
    for name, url in {
        "root": f"{base_url}/",
        "api": f"{base_url}/api/",
        "runtime_state": f"{base_url}/__gateway_runtime_state",
    }.items():
        result = _run(
            ["bash", "-lc", f"curl -s -o /dev/null -w '%{{http_code}}' {url} || true"],
            timeout=10,
        )
        public_checks[name] = (result.get("stdout") or "").strip() or "unreachable"

    return {
        "ok": True,
        "mode": "read_only",
        "config_path": str(_active_config_path()),
        "runtime_context": runtime_context,
        "address_mode": address_mode,
        "checked_base_url": base_url,
        "runtime": "local" if str(runtime_context).lower() == "local" or str(address_mode).lower() == "local" else (config.get("runtime") or config.get("runtime_mode")),
        "strategy": "direct" if str(runtime_context).lower() == "local" or str(address_mode).lower() == "local" else (config.get("strategy") or config.get("deployment_strategy")),
        "route_count": len(routes),
        "docker": docker_status,
        "public_checks": public_checks,
    }


@app.get("/__gateway_control/parity")
def control_parity(
    x_pingora_control_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_token(x_pingora_control_token)

    config = _load_config()
    routes = config.get("routes") or []
    production_security = config.get("production_security") or {}

    checks = [
        {"name": "CLI lifecycle commands", "ok": True},
        {"name": "Docker blue/green support", "ok": True},
        {"name": "Linux/systemd support", "ok": True},
        {"name": "Pingora-only edge direction", "ok": True},
        {"name": "Runtime state endpoint", "ok": bool(config.get("runtime_state", {}).get("enabled", False))},
        {"name": "Active health-check policy", "ok": any(isinstance(r, dict) and r.get("health_check") for r in routes)},
        {"name": "Decision integrity before deploy", "ok": True},
        {"name": "Backup policy", "ok": True},
        {"name": "Real TLS/ACME", "ok": bool((production_security.get("https_tls") or {}).get("enabled"))},
        {"name": "Secrets manager", "ok": bool((production_security.get("secrets_management") or {}).get("enabled"))},
        {"name": "Observability/metrics", "ok": bool((production_security.get("observability_slo") or {}).get("enabled"))},
        {"name": "Kubernetes/ECS targets", "ok": False},
    ]

    passed = sum(1 for item in checks if item["ok"])
    total = len(checks)

    return {
        "ok": True,
        "target": "Production Gateway Parity Target",
        "score": round((passed / total) * 100, 1) if total else 0,
        "checks": checks,
    }


@app.get("/__gateway_control/upstreams")
def control_upstreams(
    x_pingora_control_token: str | None = Header(default=None),
    runtime_context: str = "config_target",
    address_mode: str = "config_target",
) -> dict[str, Any]:
    _require_token(x_pingora_control_token)

    runtime_context, address_mode, _base_url = _resolve_runtime_context(runtime_context, address_mode)

    # In local mode, upstream list should reflect live effective runtime admin state,
    # not only static config inventory.
    if str(runtime_context).lower() == "local" or str(address_mode).lower() == "local":
        import json
        import urllib.request

        try:
            with urllib.request.urlopen("http://127.0.0.1:8080/__gateway_runtime_state", timeout=5) as res:
                live_state = json.loads(res.read().decode("utf-8"))

            return {
                "ok": bool(live_state.get("ok")),
                "runtime_context": runtime_context,
                "address_mode": address_mode,
                "source": "live_runtime_state",
                "routes": live_state.get("routes") or [],
            }
        except Exception as exc:
            return {
                "ok": False,
                "runtime_context": runtime_context,
                "address_mode": address_mode,
                "source": "live_runtime_state",
                "error": str(exc),
                "routes": [],
            }

    config = _load_active_config()
    routes_payload = []

    for route in config.get("routes") or []:
        if not isinstance(route, dict):
            continue

        routes_payload.append(
            {
                "path": _route_path(route),
                "type": _route_type(route),
                "algorithm": _route_algorithm(route),
                "upstreams": [
                    {**item, "address": _display_address_for_mode(item.get("address"), address_mode)}
                    for item in _upstreams(route)
                ],
            }
        )

    return {
        "ok": True,
        "runtime_context": runtime_context,
        "address_mode": address_mode,
        "source": "environment_config",
        "routes": routes_payload,
    }


@app.get("/__gateway_control/backups")
def control_backups(
    x_pingora_control_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_token(x_pingora_control_token)

    return {
        "ok": True,
        "backup_dir": str(BACKUP_DIR),
        "items": _backup_items(limit=30),
        "policy": "Backups are only for valid deployment attempts.",
    }


@app.get("/__gateway_control/backup/restore/dry-run")
def control_backup_restore_dry_run(
    x_pingora_control_token: str | None = Header(default=None),
    backup: str = "",
) -> dict[str, Any]:
    _require_token(x_pingora_control_token)

    backup_name = str(backup or "").strip()
    backup_path = BACKUP_DIR / backup_name if backup_name else None

    exists = bool(backup_path and backup_path.exists() and backup_path.is_dir())

    return {
        "ok": exists,
        "agent": "backup_restore_agent",
        "mode": "dry_run_only",
        "action": "restore_backup",
        "backup": backup_name,
        "backup_path": str(backup_path) if backup_path else None,
        "backup_exists": exists,
        "decision": "allowed" if exists else "blocked",
        "reason": "Dry-run allowed. No restore will be executed." if exists else "Backup not found.",
        "would_restore": exists,
        "requires_confirmation": True,
        "will_not_execute": True,
        "safety": {
            "restore": False,
            "restart": False,
            "docker_apply": False,
            "linux_deploy": False,
            "file_mutation": False,
            "runtime_mutation": False,
        },
        "next_step": "Run confirmed restore only with explicit confirmation after reviewing this report." if exists else "Choose an existing backup from Backup List.",
    }


@app.get("/__gateway_control/backup/restore/confirm")
def control_backup_restore_confirm(
    x_pingora_control_token: str | None = Header(default=None),
    backup: str = "",
    confirm: str = "",
) -> dict[str, Any]:
    _require_token(x_pingora_control_token)

    import os
    import shutil
    import subprocess
    import time
    import urllib.request
    from datetime import datetime, UTC

    backup_name = str(backup or "").strip()
    confirm_text = str(confirm or "").strip()
    backup_path = BACKUP_DIR / backup_name if backup_name else None

    if confirm_text != "RESTORE BACKUP":
        return {
            "ok": False,
            "agent": "backup_restore_agent",
            "mode": "confirmed_restore",
            "decision": "blocked",
            "reason": "Restore requires confirm=RESTORE BACKUP",
            "backup": backup_name,
            "requires_confirmation": True,
            "will_not_execute": True,
            "safety": {
                "restore": False,
                "file_mutation": False,
                "runtime_mutation": False,
                "restart": False,
                "docker_apply": False,
                "linux_deploy": False,
            },
        }

    if not backup_path or not backup_path.exists() or not backup_path.is_dir():
        return {
            "ok": False,
            "agent": "backup_restore_agent",
            "mode": "confirmed_restore",
            "decision": "blocked",
            "reason": "Backup not found.",
            "backup": backup_name,
            "backup_exists": False,
            "will_not_execute": True,
        }

    active_context = _read_active_runtime_context()
    runtime_context = str(active_context.get("runtime_context") or "config_target")
    address_mode = str(active_context.get("address_mode") or "config_target")

    project_dir = PROJECT_ROOT / "generated-pingora-proxy"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    safety_backup = BACKUP_DIR / f"manual-before-dashboard-restore-{stamp}"

    # Avoid rare collisions if the dashboard button is clicked twice quickly.
    suffix = 1
    while safety_backup.exists():
        safety_backup = BACKUP_DIR / f"manual-before-dashboard-restore-{stamp}-{suffix}"
        suffix += 1

    restore_lock = PROJECT_ROOT / "data" / "restore_confirm.lock"
    restore_lock.parent.mkdir(parents=True, exist_ok=True)

    try:
        lock_fd = os.open(str(restore_lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(lock_fd, str(time.time()).encode("utf-8"))
        os.close(lock_fd)
    except FileExistsError:
        return {
            "ok": False,
            "agent": "backup_restore_agent",
            "mode": "confirmed_restore",
            "decision": "blocked_restore_already_running",
            "reason": "Another confirmed restore is already running. Wait for it to finish before retrying.",
            "backup": backup_name,
            "will_not_execute": True,
        }

    report: dict[str, Any] = {
        "ok": False,
        "agent": "backup_restore_agent",
        "mode": "confirmed_restore",
        "action": "restore_backup",
        "backup": backup_name,
        "backup_path": str(backup_path),
        "runtime_context": runtime_context,
        "address_mode": address_mode,
        "confirmed": True,
        "safety_backup_created": False,
        "env_configs_regenerated": False,
        "local_runtime_restarted": False,
        "local_runtime_healthy": False,
        "dashboard_verifier_passed": False,
        "runtime_admin_verifier_passed": False,
        "steps": [],
    }

    try:
        if project_dir.exists():
            shutil.copytree(
                project_dir,
                safety_backup,
                ignore=shutil.ignore_patterns(
                    "target",
                    "tmp",
                    "logs",
                    "*.log",
                    ".DS_Store",
                ),
            )
            report["safety_backup_created"] = True
            report["safety_backup_path"] = str(safety_backup)
            report["steps"].append("safety_backup_created")

        restore_stage = PROJECT_ROOT / "tmp" / f"restore-stage-{stamp}"
        restore_previous = PROJECT_ROOT / "tmp" / f"restore-previous-{stamp}"

        if restore_stage.exists():
            shutil.rmtree(restore_stage)

        if restore_previous.exists():
            shutil.rmtree(restore_previous)

        shutil.copytree(
            backup_path,
            restore_stage,
            ignore=shutil.ignore_patterns(
                "target",
                "tmp",
                "logs",
                "*.log",
                ".DS_Store",
            ),
        )
        report["restore_stage_path"] = str(restore_stage)
        report["steps"].append("backup_copied_to_restore_stage")

        required_after_restore = [
            restore_stage / "Cargo.toml",
            restore_stage / "src" / "main.rs",
            restore_stage / "config.json",
        ]

        missing_after_restore = [
            str(item)
            for item in required_after_restore
            if not item.exists()
        ]

        if missing_after_restore:
            report["ok"] = False
            report["decision"] = "failed_restore_integrity"
            report["missing_after_restore"] = missing_after_restore
            report["reason"] = "Restore staging is missing required generated project files. Live generated project was not replaced."
            return report

        report["restore_integrity_checked"] = True
        report["steps"].append("restore_integrity_checked")

        # Transactional swap:
        # 1. Move current live project aside.
        # 2. Move verified staging into live path.
        # 3. Only delete previous copy after the verified staging is live.
        if project_dir.exists():
            project_dir.rename(restore_previous)
            report["restore_previous_path"] = str(restore_previous)
            report["steps"].append("current_project_moved_to_previous")

        restore_stage.rename(project_dir)
        report["steps"].append("verified_restore_stage_promoted")
        report["backup_restored"] = True

        if restore_previous.exists():
            shutil.rmtree(restore_previous, ignore_errors=True)
            report["steps"].append("previous_project_removed_after_successful_swap")

        env_sync = subprocess.run(
            ["python", "core/env_config_resolver.py"],
            cwd=str(PROJECT_ROOT),
            text=True,
            capture_output=True,
            timeout=60,
        )
        report["env_config_sync"] = {
            "returncode": env_sync.returncode,
            "stdout_tail": env_sync.stdout[-2000:],
            "stderr_tail": env_sync.stderr[-2000:],
        }
        report["env_configs_regenerated"] = env_sync.returncode == 0
        report["steps"].append("env_configs_regenerated")

        dashboard_regen = subprocess.run(
            [
                "python",
                "-c",
                "import json; from pathlib import Path; "
                "from core.project_writer import write_route_directory_homepage; "
                "config_path = Path('generated-pingora-proxy/config.local.json'); "
                "config = json.loads(config_path.read_text(encoding='utf-8')); "
                "write_route_directory_homepage(config, 'generated-pingora-proxy'); "
                "print('dashboard regenerated')",
            ],
            cwd=str(PROJECT_ROOT),
            text=True,
            capture_output=True,
            timeout=60,
        )
        report["dashboard_regenerated"] = dashboard_regen.returncode == 0
        report["dashboard_regen"] = {
            "returncode": dashboard_regen.returncode,
            "stdout_tail": dashboard_regen.stdout[-2000:],
            "stderr_tail": dashboard_regen.stderr[-2000:],
        }
        report["steps"].append("dashboard_regenerated")

        if runtime_context == "local" or address_mode == "local":
            subprocess.run(
                "lsof -tiTCP:8080 -sTCP:LISTEN | xargs kill -9 2>/dev/null || true",
                shell=True,
                cwd=str(PROJECT_ROOT),
                timeout=15,
            )

            start = subprocess.run(
                ["pingora-ai", "start", "--local"],
                cwd=str(PROJECT_ROOT),
                text=True,
                capture_output=True,
                timeout=120,
            )
            report["local_start"] = {
                "returncode": start.returncode,
                "stdout_tail": start.stdout[-3000:],
                "stderr_tail": start.stderr[-3000:],
            }

            report["local_runtime_restarted"] = bool(
                start.returncode == 0
                or "Local Pingora runtime start requested" in start.stdout
            )
            report["steps"].append("local_runtime_restarted")

            runtime_state_ok = False
            runtime_state_error = None
            runtime_state_tail = ""

            for attempt in range(1, 31):
                try:
                    with urllib.request.urlopen(
                        "http://127.0.0.1:8080/__gateway_runtime_state",
                        timeout=5,
                    ) as res:
                        runtime_state = res.read().decode("utf-8", errors="replace")

                    runtime_state_tail = runtime_state[-2000:]

                    if '"ok":true' in runtime_state.replace(" ", ""):
                        runtime_state_ok = True
                        report["local_runtime_health_attempts"] = attempt
                        report["steps"].append("local_runtime_health_checked")
                        break

                except Exception as exc:
                    runtime_state_error = str(exc)

                time.sleep(1)

            report["local_runtime_healthy"] = runtime_state_ok
            report["local_runtime_state_tail"] = runtime_state_tail

            if not runtime_state_ok:
                report["local_runtime_health_error"] = runtime_state_error

        runtime_admin_check = subprocess.run(
            ["python", "scripts/verify_runtime_admin_local.py"],
            cwd=str(PROJECT_ROOT),
            text=True,
            capture_output=True,
            timeout=180,
        )
        report["runtime_admin_verifier"] = {
            "returncode": runtime_admin_check.returncode,
            "stdout_tail": runtime_admin_check.stdout[-3000:],
            "stderr_tail": runtime_admin_check.stderr[-3000:],
        }
        report["runtime_admin_verifier_passed"] = runtime_admin_check.returncode == 0

        dashboard_check = subprocess.run(
            ["python", "scripts/verify_dashboard_buttons_local.py"],
            cwd=str(PROJECT_ROOT),
            text=True,
            capture_output=True,
            timeout=240,
        )
        report["dashboard_verifier"] = {
            "returncode": dashboard_check.returncode,
            "stdout_tail": dashboard_check.stdout[-3000:],
            "stderr_tail": dashboard_check.stderr[-3000:],
        }
        report["dashboard_verifier_passed"] = dashboard_check.returncode == 0

        report["ok"] = bool(
            report["safety_backup_created"]
            and report["env_configs_regenerated"]
            and report.get("dashboard_regenerated", False)
            and report.get("local_runtime_restarted", False)
            and report["local_runtime_healthy"]
            and report["runtime_admin_verifier_passed"]
            and report["dashboard_verifier_passed"]
        )
        report["decision"] = "completed" if report["ok"] else "completed_with_failed_verification"
        return report

    except Exception as exc:
        report["ok"] = False
        report["decision"] = "failed"
        report["error"] = str(exc)
        return report


@app.get("/__gateway_control/health-policy")
def control_health_policy(
    x_pingora_control_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_token(x_pingora_control_token)

    config = _load_config()
    policies = []

    for route in config.get("routes") or []:
        if not isinstance(route, dict):
            continue

        path = _route_path(route)
        if path == "/":
            continue

        policies.append(
            {
                "path": path,
                "health_check": route.get("health_check") or {},
            }
        )

    return {
        "ok": True,
        "policies": policies,
    }


# ---------------------------------------------------------------------
# Read-only role-based agent report endpoints
# ---------------------------------------------------------------------

@app.get("/__gateway_control/doctor")
def control_doctor_report(
    x_pingora_control_token: str | None = Header(default=None),
    runtime_context: str = "config_target",
    address_mode: str = "config_target",
) -> dict[str, Any]:
    _require_token(x_pingora_control_token)
    runtime_context, address_mode, _base_url = _resolve_runtime_context(runtime_context, address_mode)
    from pingora_ai.agent_reports import runtime_admin_report

    return runtime_admin_report(runtime_context=runtime_context, address_mode=address_mode)


@app.get("/__gateway_control/performance")
def control_performance_report(
    x_pingora_control_token: str | None = Header(default=None),
    requests: int = 30,
    runtime_context: str = "config_target",
    address_mode: str = "config_target",
) -> dict[str, Any]:
    _require_token(x_pingora_control_token)
    runtime_context, address_mode, _base_url = _resolve_runtime_context(runtime_context, address_mode)
    from pingora_ai.agent_reports import performance_report

    return performance_report(
        requests=requests,
        runtime_context=runtime_context,
        address_mode=address_mode,
    )


@app.get("/__gateway_control/decision-integrity")
def control_decision_integrity_report(
    x_pingora_control_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_token(x_pingora_control_token)
    from pingora_ai.agent_reports import decision_integrity_report

    return decision_integrity_report()




def _fetch_runtime_state(
    runtime_context: str | None = None,
    address_mode: str | None = None,
) -> dict[str, Any]:
    import json
    import urllib.request

    # Local dashboard/control phase: keep runtime-state reads local-only.
    url = "http://127.0.0.1:8080/__gateway_runtime_state"

    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


@app.get("/__gateway_control/health/list")
def control_health_list(
    runtime_context: str | None = None,
    address_mode: str | None = None,
) -> dict[str, Any]:
    try:
        state = _fetch_runtime_state(runtime_context=runtime_context, address_mode=address_mode)
        active_health = state.get("active_health", {})
        upstreams = active_health.get("upstreams", [])

        return {
            "ok": True,
            "command": "pingora-ai health list --local",
            "local_only": True,
            "active_health": active_health,
            "summary": {
                "total_upstreams": len(upstreams),
                "healthy_upstreams": sum(1 for item in upstreams if item.get("healthy") is True),
                "unhealthy_upstreams": sum(1 for item in upstreams if item.get("healthy") is False),
            },
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "command": "pingora-ai health list --local"}


@app.get("/__gateway_control/health/test")
def control_health_test(
    runtime_context: str | None = None,
    address_mode: str | None = None,
) -> dict[str, Any]:
    try:
        state = _fetch_runtime_state(runtime_context=runtime_context, address_mode=address_mode)
        active_health = state.get("active_health", {})
        policy = active_health.get("policy", {})
        upstreams = active_health.get("upstreams", [])

        results = []
        for item in upstreams:
            results.append({
                "route": item.get("route"),
                "upstream": item.get("upstream"),
                "healthy": item.get("healthy"),
                "last_status": item.get("last_status"),
                "consecutive_successes": item.get("consecutive_successes"),
                "consecutive_failures": item.get("consecutive_failures"),
                "last_error": item.get("last_error"),
            })

        return {
            "ok": True,
            "command": "pingora-ai health test --local",
            "local_only": True,
            "policy": policy,
            "results": results,
            "summary": {
                "total": len(results),
                "passed": sum(1 for item in results if item.get("healthy") is True),
                "failed": sum(1 for item in results if item.get("healthy") is False),
            },
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "command": "pingora-ai health test --local"}


@app.get("/__gateway_control/health/reset/dry-run")
def control_health_reset_dry_run() -> dict[str, Any]:
    return {
        "ok": True,
        "dry_run": True,
        "will_not_execute": True,
        "local_only": True,
        "command": "pingora-ai health reset --local",
        "safety": "Browser dashboard does not execute live health reset.",
        "would_set": {
            "path": "/health",
            "expected_status": 200,
            "timeout_seconds": 2,
            "interval_seconds": 5,
            "healthy_threshold": 2,
            "unhealthy_threshold": 3,
            "body_contains": None,
            "header_name": None,
            "header_value": None,
        },
    }



@app.get("/__gateway_control/dashboard-check")
def control_dashboard_check_report(
    x_pingora_control_token: str | None = Header(default=None),
    runtime_context: str = "config_target",
    address_mode: str = "config_target",
) -> dict[str, Any]:
    _require_token(x_pingora_control_token)
    runtime_context, address_mode, _base_url = _resolve_runtime_context(runtime_context, address_mode)
    from pingora_ai.agent_reports import dashboard_report

    return dashboard_report(runtime_context=runtime_context, address_mode=address_mode)


@app.get("/__gateway_control/agent-report")
def control_full_agent_report(
    x_pingora_control_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_token(x_pingora_control_token)
    from pingora_ai.agent_reports import full_agent_report

    return full_agent_report()


@app.get("/__gateway_control/lessons")
def control_lesson_learning_report(
    x_pingora_control_token: str | None = Header(default=None),
    limit: int = 10,
) -> dict[str, Any]:
    _require_token(x_pingora_control_token)
    from pingora_ai.agent_reports import lesson_learning_report

    return lesson_learning_report(limit=limit)




# ---------------------------------------------------------------------
# Cache Control Agent local-only endpoints
# ---------------------------------------------------------------------
# Dry-run never mutates runtime. Confirmed clear is local-only first.

@app.get("/__gateway_control/cache/clear/dry-run")
def control_cache_clear_dry_run(
    x_pingora_control_token: str | None = Header(default=None),
    runtime_context: str = "local",
    address_mode: str = "local",
) -> dict[str, Any]:
    _require_token(x_pingora_control_token)

    runtime_context, address_mode, base_url = _resolve_runtime_context(runtime_context, address_mode)
    local_allowed = runtime_context == "local" and address_mode == "local"

    return {
        "ok": local_allowed,
        "agent": "cache_control_agent",
        "mode": "dry_run_only",
        "action": "clear_cache",
        "runtime_context": runtime_context,
        "address_mode": address_mode,
        "runtime_url": base_url,
        "decision": "allowed" if local_allowed else "blocked",
        "reason": "Dry-run allowed. No cache entries will be cleared." if local_allowed else "Cache clear is local-only in this milestone.",
        "would_clear_cache": local_allowed,
        "requires_confirmation": True,
        "will_not_execute": True,
        "local_only": True,
        "safety": {
            "runtime_mutation": False,
            "file_mutation": False,
            "docker_apply": False,
            "linux_deploy": False,
            "restart": False,
            "rollback": False,
        },
        "next_step": "Run confirmed clear only after reviewing this dry-run report." if local_allowed else "Switch to local runtime context first.",
    }


@app.get("/__gateway_control/cache/clear")
def control_cache_clear_confirm(
    x_pingora_control_token: str | None = Header(default=None),
    confirm: bool = False,
    runtime_context: str = "local",
    address_mode: str = "local",
) -> dict[str, Any]:
    _require_token(x_pingora_control_token)

    runtime_context, address_mode, base_url = _resolve_runtime_context(runtime_context, address_mode)

    if runtime_context != "local" or address_mode != "local":
        return {
            "ok": False,
            "agent": "cache_control_agent",
            "mode": "confirmed_live_local",
            "action": "clear_cache",
            "decision": "blocked",
            "reason": "Cache clear is local-only in this milestone.",
            "runtime_context": runtime_context,
            "address_mode": address_mode,
            "requires_confirmation": True,
            "will_not_execute": True,
            "local_only": True,
        }

    if confirm is not True:
        return {
            "ok": False,
            "agent": "cache_control_agent",
            "mode": "confirmed_live_local",
            "action": "clear_cache",
            "decision": "blocked",
            "reason": "Cache clear requires confirm=true.",
            "runtime_context": runtime_context,
            "address_mode": address_mode,
            "requires_confirmation": True,
            "will_not_execute": True,
            "local_only": True,
            "safety": {
                "runtime_mutation": False,
                "file_mutation": False,
                "docker_apply": False,
                "linux_deploy": False,
                "restart": False,
                "rollback": False,
            },
        }

    import json
    import urllib.error
    import urllib.request

    url = f"{base_url.rstrip('/')}/__gateway_runtime_admin/cache/clear"
    req = urllib.request.Request(url, data=b"", method="POST")

    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            body = res.read().decode("utf-8", errors="replace")
            try:
                runtime_report = json.loads(body)
            except Exception:
                runtime_report = {"raw": body}

        return {
            "ok": bool(runtime_report.get("ok")),
            "agent": "cache_control_agent",
            "mode": "confirmed_live_local",
            "action": "clear_cache",
            "decision": "completed" if runtime_report.get("ok") else "failed",
            "runtime_context": runtime_context,
            "address_mode": address_mode,
            "runtime_url": url,
            "confirmed": True,
            "local_only": True,
            "live_runtime_mutation": True,
            "cleared_entries": runtime_report.get("cleared_entries"),
            "runtime_report": runtime_report,
            "docker_apply": False,
            "linux_deploy": False,
            "restart": False,
            "rollback": False,
        }

    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {
            "ok": False,
            "agent": "cache_control_agent",
            "mode": "confirmed_live_local",
            "action": "clear_cache",
            "decision": "failed",
            "runtime_url": url,
            "error": f"HTTP {exc.code}: {body}",
            "live_runtime_mutation": False,
            "docker_apply": False,
            "linux_deploy": False,
            "restart": False,
            "rollback": False,
        }
    except Exception as exc:
        return {
            "ok": False,
            "agent": "cache_control_agent",
            "mode": "confirmed_live_local",
            "action": "clear_cache",
            "decision": "failed",
            "runtime_url": url,
            "error": str(exc),
            "live_runtime_mutation": False,
            "docker_apply": False,
            "linux_deploy": False,
            "restart": False,
            "rollback": False,
        }

# ---------------------------------------------------------------------
# Upstream State Agent dry-run endpoints
# ---------------------------------------------------------------------
# These endpoints DO NOT mutate runtime/config/Docker/Linux.
# They only return agent decision reports.

@app.get("/__gateway_control/upstream/disable/dry-run")
def control_upstream_disable_dry_run(
    x_pingora_control_token: str | None = Header(default=None),
    route: str = "/api",
    upstream: str = "host.docker.internal:9101",
    runtime_context: str = "config_target",
    address_mode: str = "config_target",
) -> dict[str, Any]:
    _require_token(x_pingora_control_token)
    runtime_context, address_mode, _base_url = _resolve_runtime_context(runtime_context, address_mode)
    from pingora_ai.agent_reports import upstream_mutation_dry_run

    return upstream_mutation_dry_run(action="disable", route_path=route, upstream=upstream, runtime_context=runtime_context, address_mode=address_mode)


@app.get("/__gateway_control/upstream/drain/dry-run")
def control_upstream_drain_dry_run(
    x_pingora_control_token: str | None = Header(default=None),
    route: str = "/api",
    upstream: str = "host.docker.internal:9101",
    runtime_context: str = "config_target",
    address_mode: str = "config_target",
) -> dict[str, Any]:
    _require_token(x_pingora_control_token)
    runtime_context, address_mode, _base_url = _resolve_runtime_context(runtime_context, address_mode)
    from pingora_ai.agent_reports import upstream_mutation_dry_run

    return upstream_mutation_dry_run(action="drain", route_path=route, upstream=upstream, runtime_context=runtime_context, address_mode=address_mode)


@app.get("/__gateway_control/upstream/enable/dry-run")
def control_upstream_enable_dry_run(
    x_pingora_control_token: str | None = Header(default=None),
    route: str = "/api",
    upstream: str = "host.docker.internal:9101",
    runtime_context: str = "config_target",
    address_mode: str = "config_target",
) -> dict[str, Any]:
    _require_token(x_pingora_control_token)
    runtime_context, address_mode, _base_url = _resolve_runtime_context(runtime_context, address_mode)
    from pingora_ai.agent_reports import upstream_mutation_dry_run

    return upstream_mutation_dry_run(action="enable", route_path=route, upstream=upstream, runtime_context=runtime_context, address_mode=address_mode)


@app.get("/__gateway_control/upstream/weight/dry-run")
def control_upstream_weight_dry_run(
    x_pingora_control_token: str | None = Header(default=None),
    route: str = "/api",
    upstream: str = "host.docker.internal:9101",
    runtime_context: str = "config_target",
    address_mode: str = "config_target",
    weight: int = 1,
) -> dict[str, Any]:
    _require_token(x_pingora_control_token)
    runtime_context, address_mode, _base_url = _resolve_runtime_context(runtime_context, address_mode)
    from pingora_ai.agent_reports import upstream_mutation_dry_run

    return upstream_mutation_dry_run(action="weight", route_path=route, upstream=upstream, weight=weight, runtime_context=runtime_context, address_mode=address_mode)


@app.get("/__gateway_control/upstream/promote-backup/dry-run")
def control_upstream_promote_backup_dry_run(
    x_pingora_control_token: str | None = Header(default=None),
    route: str = "/api",
    upstream: str = "host.docker.internal:9101",
    runtime_context: str = "config_target",
    address_mode: str = "config_target",
) -> dict[str, Any]:
    _require_token(x_pingora_control_token)
    runtime_context, address_mode, _base_url = _resolve_runtime_context(runtime_context, address_mode)
    from pingora_ai.agent_reports import upstream_mutation_dry_run

    return upstream_mutation_dry_run(action="promote-backup", route_path=route, upstream=upstream, runtime_context=runtime_context, address_mode=address_mode)


# ---------------------------------------------------------------------
# Runtime Admin Agent confirmed local endpoints
# ---------------------------------------------------------------------
# Local phase only. These call the live local Pingora runtime admin API.
# They require confirm=true and do not run Docker apply, Linux deploy, restart, or rollback.

def _runtime_admin_agent_call_local(action: str, route: str, upstream: str, confirm: bool) -> dict[str, Any]:
    import json
    import urllib.parse
    import urllib.request
    import urllib.error

    if not confirm:
        return {
            "ok": False,
            "agent": "runtime_admin_agent",
            "mode": "confirmed_live_local",
            "action": action,
            "decision": "blocked",
            "reason": "confirm=true is required",
            "live_runtime_mutation": False,
            "docker_apply": False,
            "linux_deploy": False,
            "restart": False,
            "rollback": False,
        }

    route = str(route or "/api")
    upstream = str(upstream or "")

    if not upstream:
        return {
            "ok": False,
            "agent": "runtime_admin_agent",
            "mode": "confirmed_live_local",
            "action": action,
            "decision": "blocked",
            "reason": "upstream is required",
            "live_runtime_mutation": False,
            "docker_apply": False,
            "linux_deploy": False,
            "restart": False,
            "rollback": False,
        }

    query = urllib.parse.urlencode(
        {
            "route": route,
            "upstream": upstream,
            "confirm": "true",
        }
    )

    url = f"http://127.0.0.1:8080/__gateway_runtime_admin/upstream/{action}?{query}"

    try:
        req = urllib.request.Request(url, data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=10) as res:
            runtime_result = json.loads(res.read().decode("utf-8"))

        return {
            "ok": bool(runtime_result.get("ok")),
            "agent": "runtime_admin_agent",
            "mode": "confirmed_live_local",
            "action": action,
            "route": route,
            "upstream": upstream,
            "runtime_url": url,
            "runtime_result": runtime_result,
            "live_runtime_mutation": bool(runtime_result.get("live_runtime_mutation")),
            "docker_apply": False,
            "linux_deploy": False,
            "restart": False,
            "rollback": False,
        }
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {
            "ok": False,
            "agent": "runtime_admin_agent",
            "mode": "confirmed_live_local",
            "action": action,
            "route": route,
            "upstream": upstream,
            "runtime_url": url,
            "error": f"HTTP {exc.code}: {body}",
            "live_runtime_mutation": False,
            "docker_apply": False,
            "linux_deploy": False,
            "restart": False,
            "rollback": False,
        }
    except Exception as exc:
        return {
            "ok": False,
            "agent": "runtime_admin_agent",
            "mode": "confirmed_live_local",
            "action": action,
            "route": route,
            "upstream": upstream,
            "runtime_url": url,
            "error": str(exc),
            "live_runtime_mutation": False,
            "docker_apply": False,
            "linux_deploy": False,
            "restart": False,
            "rollback": False,
        }


@app.get("/__gateway_control/runtime-admin/upstream/disable")
def control_runtime_admin_disable(
    x_pingora_control_token: str | None = Header(default=None),
    route: str = "/api",
    upstream: str = "127.0.0.1:9101",
    confirm: bool = False,
    runtime_context: str = "local",
    address_mode: str = "local",
) -> dict[str, Any]:
    _require_token(x_pingora_control_token)
    return _runtime_admin_agent_call_local("disable", route, upstream, confirm)


@app.get("/__gateway_control/runtime-admin/upstream/drain")
def control_runtime_admin_drain(
    x_pingora_control_token: str | None = Header(default=None),
    route: str = "/api",
    upstream: str = "127.0.0.1:9101",
    confirm: bool = False,
    runtime_context: str = "local",
    address_mode: str = "local",
) -> dict[str, Any]:
    _require_token(x_pingora_control_token)
    return _runtime_admin_agent_call_local("drain", route, upstream, confirm)


@app.get("/__gateway_control/runtime-admin/upstream/enable")
def control_runtime_admin_enable(
    x_pingora_control_token: str | None = Header(default=None),
    route: str = "/api",
    upstream: str = "127.0.0.1:9101",
    confirm: bool = False,
    runtime_context: str = "local",
    address_mode: str = "local",
) -> dict[str, Any]:
    _require_token(x_pingora_control_token)
    return _runtime_admin_agent_call_local("enable", route, upstream, confirm)


def _learning_actionability(root_cause: str, repair_action: str | None) -> dict[str, Any]:
    root = str(root_cause or "")
    action = str(repair_action or "")

    if not action or action.lower() == "none":
        return {
            "actionability": "read_only",
            "safe_to_auto_repair": False,
            "requires_confirmation": False,
            "suggested_action": None,
            "safe_command_preview": "No repair command. Learning is informational only.",
            "reason": "No repair action is registered for this learning.",
        }

    manual_actions = {
        "start_fastapi_planner": "Start the FastAPI planner service, then verify /health.",
        "start_missing_demo_backends_for_config": "Start required local demo backend ports, then rerun verification.",
    }

    confirmation_actions = {
        "upload_static_assets_to_release": "Requires confirmed release/static asset sync before changing bare-metal release state.",
        "restore_backup": "Requires explicit restore confirmation.",
        "rewrite_docker_host_to_loopback": "Requires environment-specific config confirmation.",
        "kill_process_then_reset_failed": "Requires process-control confirmation.",
    }

    auto_actions = {
        "set_local_readiness_to_homepage": "Update local readiness logic to use homepage check.",
        "set_local_live_url_to_8090": "Update local verifier URL contract.",
        "accept_docker_http_redirect_or_use_edge_health": "Update verifier health gate to accept redirects or use edge health.",
        "verify_upsert_second": "Verifier-only learning store check.",
    }

    if action in manual_actions:
        return {
            "actionability": "manual",
            "safe_to_auto_repair": False,
            "requires_confirmation": True,
            "suggested_action": action,
            "safe_command_preview": "pingora-ai start --local",
            "reason": manual_actions[action],
        }

    if action in confirmation_actions:
        return {
            "actionability": "needs_confirmation",
            "safe_to_auto_repair": False,
            "requires_confirmation": True,
            "suggested_action": action,
            "safe_command_preview": f"DRY RUN ONLY: {action}",
            "reason": confirmation_actions[action],
        }

    if action in auto_actions:
        return {
            "actionability": "auto_repairable",
            "safe_to_auto_repair": True,
            "requires_confirmation": False,
            "suggested_action": action,
            "safe_command_preview": f"Safe deterministic repair candidate: {action}",
            "reason": auto_actions[action],
        }

    return {
        "actionability": "manual",
        "safe_to_auto_repair": False,
        "requires_confirmation": True,
        "suggested_action": action,
        "safe_command_preview": f"Manual review required before running: {action}",
        "reason": f"No allowlisted auto-repair policy exists yet for root cause {root}.",
    }


def _recent_agent_learnings(limit: int = 10) -> dict[str, Any]:
    import sqlite3
    from pathlib import Path

    db = Path("data/reliability_reports.sqlite3")
    if not db.exists():
        return {
            "ok": False,
            "agent": "agent_learning_dashboard",
            "error": "data/reliability_reports.sqlite3 not found",
            "learnings": [],
        }

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row

    try:
        rows = con.execute(
            """
            SELECT
                root_cause,
                successful_repair_count,
                failed_repair_count,
                last_repair_action,
                last_verification_level,
                last_seen_at,
                notes
            FROM root_cause_learnings
            ORDER BY last_seen_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except Exception as exc:
        con.close()
        return {
            "ok": False,
            "agent": "agent_learning_dashboard",
            "error": str(exc),
            "learnings": [],
        }

    learnings = []
    for row in rows:
        item = dict(row)
        item.update(
            _learning_actionability(
                str(item.get("root_cause") or ""),
                item.get("last_repair_action"),
            )
        )
        learnings.append(item)

    con.close()

    return {
        "ok": True,
        "agent": "agent_learning_dashboard",
        "mode": "read_only",
        "source": str(db),
        "count": len(learnings),
        "learnings": learnings,
    }


@app.get("/__gateway_control/agent/learnings")
def agent_learnings(limit: int = 10) -> dict[str, Any]:
    limit = max(1, min(int(limit or 10), 50))
    return _recent_agent_learnings(limit=limit)

def _agent_repair_plan_for_learning(root_cause: str, repair_action: str | None = None) -> dict[str, Any]:
    root = str(root_cause or "unknown")
    action = str(repair_action or "")

    base = {
        "root_cause": root,
        "repair_action": action or None,
        "will_execute": False,
        "docker_apply": False,
        "linux_deploy": False,
        "restart": False,
        "rollback": False,
        "config_write": False,
        "runtime_mutation": False,
    }

    plans = {
        "docker_backend_port_unreachable": {
            "actionability": "manual",
            "risk": "medium",
            "requires_confirmation": True,
            "repair_steps": [
                "Read expected upstream backend ports from active config.",
                "Check whether required demo backend ports are listening.",
                "Start missing local demo backend processes only after confirmation.",
                "Verify gateway route liveness after backend recovery.",
            ],
            "command_preview": "pingora-ai start --local",
            "verification_command": "python scripts/verify_local_production_readiness_gate.py",
            "reason": "Docker/backend failures often require host process context and should not be auto-executed from dashboard.",
        },
        "fastapi_planner_not_running": {
            "actionability": "manual",
            "risk": "low",
            "requires_confirmation": True,
            "repair_steps": [
                "Check whether FastAPI planner port 8000 is listening.",
                "Start planner service using the project-approved command.",
                "Verify planner /health endpoint.",
                "Retry CLI planning command.",
            ],
            "command_preview": "pingora-ai api start",
            "verification_command": "curl -s http://127.0.0.1:8000/health",
            "reason": "Planner startup is process control and should be confirmed by the user.",
        },
        "bare_metal_static_homepage_missing": {
            "actionability": "needs_confirmation",
            "risk": "high",
            "requires_confirmation": True,
            "repair_steps": [
                "Confirm active bare-metal release path.",
                "Create a backup of current release assets.",
                "Upload generated static dashboard assets to active release.",
                "Verify homepage and route directory.",
            ],
            "command_preview": "DRY RUN ONLY: upload_static_assets_to_release",
            "verification_command": "pingora-ai status --linux",
            "reason": "Bare-metal release changes can affect production-style serving and require confirmation.",
        },
        "docker_http_redirect_health_gate_false_negative": {
            "actionability": "auto_repairable",
            "risk": "low",
            "requires_confirmation": False,
            "repair_steps": [
                "Update health readiness logic to accept HTTP redirect statuses 301/302/307/308.",
                "Prefer /__edge_health for Docker edge health where available.",
                "Run Docker health verifier.",
            ],
            "command_preview": "Safe deterministic verifier repair candidate: accept_docker_http_redirect_or_use_edge_health",
            "verification_command": "python scripts/verify_tls_auth_secrets.py",
            "reason": "This is verifier logic only and does not mutate live traffic.",
        },
        "verify_agent_learning_store_local": {
            "actionability": "read_only",
            "risk": "low",
            "requires_confirmation": False,
            "repair_steps": [
                "No repair needed.",
                "This learning confirms duplicate-signature upsert verification.",
            ],
            "command_preview": "python scripts/verify_agent_learning_store_local.py",
            "verification_command": "python scripts/verify_agent_learning_store_local.py",
            "reason": "Verifier-only learning; informational.",
        },
    }

    plan = plans.get(root)
    if not plan:
        plan = {
            "actionability": "manual",
            "risk": "medium",
            "requires_confirmation": True,
            "repair_steps": [
                "Review stored learning and evidence.",
                "Generate a manual repair plan.",
                "Run the smallest relevant verifier before any change.",
            ],
            "command_preview": f"Manual review required before running: {action or root}",
            "verification_command": "python scripts/verify_local_production_readiness_gate.py",
            "reason": "No allowlisted dry-run repair plan exists yet for this root cause.",
        }

    return {**base, **plan}


@app.get("/__gateway_control/agent/repair-plan/dry-run")
def agent_repair_plan_dry_run(
    x_pingora_control_token: str | None = Header(default=None),
    root_cause: str = "",
    repair_action: str | None = None,
) -> dict[str, Any]:
    _require_token(x_pingora_control_token)

    plan = _agent_repair_plan_for_learning(root_cause, repair_action)

    payload = {
        "ok": True,
        "agent": "repair_plan_agent",
        "mode": "dry_run_only",
        "safety": {
            "will_execute": False,
            "docker_apply": False,
            "linux_deploy": False,
            "restart": False,
            "rollback": False,
            "config_write": False,
            "runtime_mutation": False,
        },
        "plan": plan,
    }

    _store_repair_plan_history(payload)
    return payload

def _repair_plan_history_db_path() -> Path:
    return Path("data") / "repair_plan_history.sqlite3"


def _init_repair_plan_history_db() -> None:
    import sqlite3

    db_path = _repair_plan_history_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(db_path)
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS repair_plan_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                root_cause TEXT NOT NULL,
                actionability TEXT,
                risk TEXT,
                requires_confirmation INTEGER NOT NULL DEFAULT 0,
                command_preview TEXT,
                verification_command TEXT,
                will_execute INTEGER NOT NULL DEFAULT 0,
                mode TEXT NOT NULL DEFAULT 'dry_run_only',
                plan_json TEXT NOT NULL
            )
            """
        )
        con.commit()
    finally:
        con.close()


def _store_repair_plan_history(plan_payload: dict[str, Any]) -> None:
    import json
    import sqlite3
    from datetime import datetime, timezone

    _init_repair_plan_history_db()

    plan = plan_payload.get("plan") or {}
    db_path = _repair_plan_history_db_path()

    con = sqlite3.connect(db_path)
    try:
        con.execute(
            """
            INSERT INTO repair_plan_history (
                created_at,
                root_cause,
                actionability,
                risk,
                requires_confirmation,
                command_preview,
                verification_command,
                will_execute,
                mode,
                plan_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                str(plan.get("root_cause") or "unknown"),
                str(plan.get("actionability") or ""),
                str(plan.get("risk") or ""),
                1 if plan.get("requires_confirmation") is True else 0,
                str(plan.get("command_preview") or ""),
                str(plan.get("verification_command") or ""),
                1 if plan.get("will_execute") is True else 0,
                str(plan_payload.get("mode") or "dry_run_only"),
                json.dumps(plan_payload, sort_keys=True),
            ),
        )
        con.commit()
    finally:
        con.close()


def _recent_repair_plan_history(limit: int = 10) -> dict[str, Any]:
    import json
    import sqlite3

    limit = max(1, min(int(limit or 10), 50))
    db_path = _repair_plan_history_db_path()

    if not db_path.exists():
        return {
            "ok": True,
            "agent": "repair_plan_history_agent",
            "mode": "read_only",
            "source": str(db_path),
            "count": 0,
            "history": [],
        }

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    try:
        rows = con.execute(
            """
            SELECT
                id,
                created_at,
                root_cause,
                actionability,
                risk,
                requires_confirmation,
                command_preview,
                verification_command,
                will_execute,
                mode,
                plan_json
            FROM repair_plan_history
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        con.close()

    history = []
    for row in rows:
        item = dict(row)
        try:
            item["plan"] = json.loads(item.get("plan_json") or "{}")
        except Exception:
            item["plan"] = {}
        item.pop("plan_json", None)
        item["requires_confirmation"] = bool(item.get("requires_confirmation"))
        item["will_execute"] = bool(item.get("will_execute"))
        history.append(item)

    return {
        "ok": True,
        "agent": "repair_plan_history_agent",
        "mode": "read_only",
        "source": str(db_path),
        "count": len(history),
        "history": history,
    }


@app.get("/__gateway_control/agent/repair-plan/history")
def agent_repair_plan_history(
    x_pingora_control_token: str | None = Header(default=None),
    limit: int = 10,
) -> dict[str, Any]:
    _require_token(x_pingora_control_token)
    return _recent_repair_plan_history(limit=limit)


