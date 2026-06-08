from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path


CONTROL = "http://127.0.0.1:8787"
RUNTIME = "http://127.0.0.1:8080"
TOKEN = "dev-local-control-token"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def get_json(url: str, token: bool = False, timeout: int = 30) -> dict:
    headers = {}
    if token:
        headers["X-Pingora-Control-Token"] = TOKEN

    req = urllib.request.Request(url, headers=headers)

    with urllib.request.urlopen(req, timeout=timeout) as res:
        body = res.read().decode("utf-8", errors="replace")

    try:
        return json.loads(body)
    except Exception as exc:
        raise AssertionError(f"Invalid JSON from {url}: {body[:500]}") from exc


def control(endpoint: str, timeout: int = 60) -> dict:
    return get_json(f"{CONTROL}{endpoint}", token=True, timeout=timeout)


def main() -> int:
    print("🧪 Verifying dashboard Control API endpoints for local runtime")

    runtime_state = get_json(f"{RUNTIME}/__gateway_runtime_state")
    require(runtime_state.get("ok") is True, "Runtime state not ok")
    print("✅ Runtime state endpoint correct")

    health = get_json(f"{CONTROL}/health")
    require(health.get("ok") is True, "Control API health not ok")
    print("✅ Control API health endpoint correct")

    status = control("/__gateway_control/status?runtime_context=local&address_mode=local")
    require(status.get("ok") is True, "Local status not ok")
    require(status.get("runtime_context") == "local", "Status runtime_context not local")
    require(status.get("address_mode") == "local", "Status address_mode not local")
    require(status.get("checked_base_url") == RUNTIME, "Status checked wrong base URL")
    require(status.get("public_checks", {}).get("root") == "200", "Status root check not 200")
    require(status.get("public_checks", {}).get("api") == "200", "Status api check not 200")
    require(status.get("public_checks", {}).get("runtime_state") == "200", "Status runtime_state check not 200")
    print("✅ Local Status endpoint correct")

    upstreams = control("/__gateway_control/upstreams?runtime_context=local&address_mode=local")
    require(upstreams.get("ok") is True, "Upstream List not ok")
    require(upstreams.get("runtime_context") == "local", "Upstream runtime_context not local")
    require(upstreams.get("address_mode") == "local", "Upstream address_mode not local")
    require(upstreams.get("source") == "live_runtime_state", "Upstream source should be live_runtime_state")
    upstream_text = json.dumps(upstreams)
    require("127.0.0.1:9101" in upstream_text, "Missing local upstream 127.0.0.1:9101")
    require("host.docker.internal" not in upstream_text, "Local upstream endpoint leaked Docker address")
    print("✅ Upstream List endpoint correct")

    dashboard = control("/__gateway_control/dashboard-check?runtime_context=local&address_mode=local")
    require(dashboard.get("ok") is True, "Dashboard Check not ok")
    require(dashboard.get("runtime_context") == "local", "Dashboard Check runtime_context not local")
    require(dashboard.get("address_mode") == "local", "Dashboard Check address_mode not local")
    require(dashboard.get("checked_base_url") == RUNTIME, "Dashboard Check wrong base URL")
    require(dashboard.get("dashboard_status") == 200, "Dashboard status not 200")
    markers = dashboard.get("dashboard_markers") or {}
    missing = [name for name, ok in markers.items() if not ok]
    require(not missing, f"Dashboard Check missing markers: {missing}")
    print("✅ Dashboard Check endpoint correct")

    performance = control("/__gateway_control/performance?requests=20&runtime_context=local&address_mode=local", timeout=90)
    require(performance.get("ok") is True, "Performance endpoint not ok")
    require(performance.get("runtime_context") == "local", "Performance runtime_context not local")
    require(performance.get("address_mode") == "local", "Performance address_mode not local")
    require(performance.get("environment") == "local", "Performance environment not local")
    require(performance.get("checked_base_url") == RUNTIME, "Performance checked wrong base URL")
    require(performance.get("success_count") == 20, "Performance success_count not 20")
    require(performance.get("success_rate") == 100, "Performance success_rate not 100")
    print("✅ Performance endpoint correct")

    backups = control("/__gateway_control/backups")
    require(backups.get("ok") is True, "Backup List not ok")
    require(isinstance(backups.get("items"), list), "Backup List items not list")
    require(bool(backups.get("items")), "Backup List empty")
    print("✅ Backup List endpoint correct")

    latest_backup = str(backups["items"][0].get("name") or "")
    require(latest_backup, "Latest backup has no name")

    quoted_backup = urllib.parse.quote(latest_backup)

    restore_dry_run = control(f"/__gateway_control/backup/restore/dry-run?backup={quoted_backup}")
    require(restore_dry_run.get("ok") is True, "Restore Dry Run not ok")
    require(restore_dry_run.get("mode") == "dry_run_only", "Restore Dry Run mode wrong")
    require(restore_dry_run.get("decision") == "allowed", "Restore Dry Run decision not allowed")
    require(restore_dry_run.get("will_not_execute") is True, "Restore Dry Run should not execute")
    safety = restore_dry_run.get("safety") or {}
    require(safety.get("restore") is False, "Restore Dry Run safety.restore should be false")
    require(safety.get("file_mutation") is False, "Restore Dry Run safety.file_mutation should be false")
    require(safety.get("runtime_mutation") is False, "Restore Dry Run safety.runtime_mutation should be false")
    print("✅ Restore Backup Dry Run endpoint correct")

    restore_missing = control("/__gateway_control/backup/restore/dry-run?backup=does-not-exist")
    require(restore_missing.get("ok") is False, "Missing Restore Dry Run should not be ok")
    require(restore_missing.get("decision") == "blocked", "Missing Restore Dry Run should be blocked")
    require(restore_missing.get("will_not_execute") is True, "Missing Restore Dry Run should not execute")
    print("✅ Missing Restore Backup Dry Run endpoint correct")

    blocked_confirm = control(f"/__gateway_control/backup/restore/confirm?backup={quoted_backup}", timeout=30)
    require(blocked_confirm.get("ok") is False, "Confirmed restore without confirm should be blocked")
    require(blocked_confirm.get("decision") == "blocked", "Confirmed restore without confirm wrong decision")
    require(blocked_confirm.get("will_not_execute") is True, "Confirmed restore without confirm should not execute")
    print("✅ Confirmed Restore blocked-without-confirm endpoint correct")

    for action in ["disable", "drain", "enable"]:
        endpoint = (
            f"/__gateway_control/upstream/{action}/dry-run"
            f"?route=/api&upstream=127.0.0.1:9101"
            f"&runtime_context=local&address_mode=local"
        )
        result = control(endpoint)
        require(result.get("ok") is True, f"{action} dry-run not ok")
        require(result.get("mode") == "dry_run_only", f"{action} dry-run mode wrong")
        require(result.get("runtime_context") == "local", f"{action} dry-run runtime_context not local")
        require(result.get("address_mode") == "local", f"{action} dry-run address_mode not local")
        require(result.get("will_not_execute") is True, f"{action} dry-run should not execute")
        safety = result.get("safety") or {}
        require(safety.get("live_runtime_mutation") is False, f"{action} dry-run should not mutate runtime")
        require(safety.get("docker_apply") is False, f"{action} dry-run should not docker apply")
        require(safety.get("linux_deploy") is False, f"{action} dry-run should not linux deploy")
        print(f"✅ Upstream {action.title()} Dry Run endpoint correct")

    print("✅ DASHBOARD CONTROL ENDPOINT VERIFIER PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
