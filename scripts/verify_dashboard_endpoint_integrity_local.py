from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any


CONTROL = "http://127.0.0.1:8787"
RUNTIME = "http://127.0.0.1:8080"
TOKEN = "dev-local-control-token"


def fail(message: str) -> None:
    raise AssertionError(message)


def require(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def get_json(url: str, token: bool = False, timeout: int = 60) -> dict[str, Any]:
    headers = {}
    if token:
        headers["X-Pingora-Control-Token"] = TOKEN

    req = urllib.request.Request(url, headers=headers)

    with urllib.request.urlopen(req, timeout=timeout) as res:
        status = res.status
        body = res.read().decode("utf-8", errors="replace")

    require(status == 200, f"Expected HTTP 200 from {url}, got {status}")

    try:
        parsed = json.loads(body)
    except Exception as exc:
        raise AssertionError(f"Invalid JSON from {url}: {body[:500]}") from exc

    require(isinstance(parsed, dict), f"JSON response from {url} is not object")
    return parsed


def control(endpoint: str, timeout: int = 60) -> dict[str, Any]:
    return get_json(f"{CONTROL}{endpoint}", token=True, timeout=timeout)


def assert_no_docker_leak(data: dict[str, Any], label: str) -> None:
    text = json.dumps(data)
    require("host.docker.internal" not in text, f"{label} leaked Docker address into local result")


def assert_has_local_upstream(data: dict[str, Any], label: str) -> None:
    text = json.dumps(data)
    require("127.0.0.1:9101" in text or "127.0.0.1:9102" in text, f"{label} missing local upstream evidence")


def assert_local_context(data: dict[str, Any], label: str) -> None:
    if "runtime_context" in data:
        require(data.get("runtime_context") == "local", f"{label} runtime_context should be local")
    if "address_mode" in data:
        require(data.get("address_mode") == "local", f"{label} address_mode should be local")
    if "checked_base_url" in data:
        require(data.get("checked_base_url") == RUNTIME, f"{label} checked_base_url should be {RUNTIME}")


def assert_safety_false(safety: dict[str, Any], fields: list[str], label: str) -> None:
    require(isinstance(safety, dict), f"{label} safety must be object")
    for field in fields:
        require(safety.get(field) is False, f"{label} safety.{field} must be false")


def main() -> int:
    print("🧪 Verifying dashboard endpoint response integrity for local runtime")

    runtime = get_json(f"{RUNTIME}/__gateway_runtime_state")
    require(runtime.get("ok") is True, "Runtime state ok must be true")
    require(isinstance(runtime.get("routes"), list), "Runtime state routes must be list")
    assert_has_local_upstream(runtime, "runtime_state")
    assert_no_docker_leak(runtime, "runtime_state")
    print("✅ Runtime state response integrity correct")

    health = get_json(f"{CONTROL}/health")
    require(health.get("ok") is True, "Control health ok must be true")
    print("✅ Control health response integrity correct")

    status = control("/__gateway_control/status?runtime_context=local&address_mode=local")
    require(status.get("ok") is True, "Local Status ok must be true")
    assert_local_context(status, "Local Status")
    assert_no_docker_leak(status, "Local Status")
    checks = status.get("public_checks") or {}
    require(checks.get("root") == "200", "Local Status root check must be 200")
    require(checks.get("api") == "200", "Local Status api check must be 200")
    require(checks.get("runtime_state") == "200", "Local Status runtime_state check must be 200")
    print("✅ Local Status response integrity correct")

    upstreams = control("/__gateway_control/upstreams?runtime_context=local&address_mode=local")
    require(upstreams.get("ok") is True, "Upstream List ok must be true")
    assert_local_context(upstreams, "Upstream List")
    require(upstreams.get("source") == "live_runtime_state", "Upstream List source must be live_runtime_state")
    assert_has_local_upstream(upstreams, "Upstream List")
    assert_no_docker_leak(upstreams, "Upstream List")
    print("✅ Upstream List response integrity correct")

    dashboard = control("/__gateway_control/dashboard-check?runtime_context=local&address_mode=local")
    require(dashboard.get("ok") is True, "Dashboard Check ok must be true")
    assert_local_context(dashboard, "Dashboard Check")
    require(dashboard.get("dashboard_status") == 200, "Dashboard status must be 200")
    markers = dashboard.get("dashboard_markers") or {}
    require(isinstance(markers, dict), "dashboard_markers must be object")
    missing_markers = [name for name, ok in markers.items() if ok is not True]
    require(not missing_markers, f"Dashboard markers missing/false: {missing_markers}")
    print("✅ Dashboard Check response integrity correct")

    performance = control("/__gateway_control/performance?requests=20&runtime_context=local&address_mode=local", timeout=90)
    require(performance.get("ok") is True, "Performance ok must be true")
    assert_local_context(performance, "Performance")
    require(performance.get("environment") == "local", "Performance environment must be local")
    require(performance.get("success_count") == 20, "Performance success_count must be 20")
    require(performance.get("success_rate") == 100, "Performance success_rate must be 100")
    assert_no_docker_leak(performance, "Performance")
    print("✅ Performance response integrity correct")

    backups = control("/__gateway_control/backups")
    require(backups.get("ok") is True, "Backup List ok must be true")
    require(isinstance(backups.get("items"), list), "Backup List items must be list")
    require(len(backups["items"]) > 0, "Backup List must not be empty")
    latest_backup = str(backups["items"][0].get("name") or "")
    require(latest_backup, "Latest backup name missing")
    print("✅ Backup List response integrity correct")

    quoted_backup = urllib.parse.quote(latest_backup)

    restore_dry = control(f"/__gateway_control/backup/restore/dry-run?backup={quoted_backup}")
    require(restore_dry.get("ok") is True, "Restore Dry Run ok must be true")
    require(restore_dry.get("agent") == "backup_restore_agent", "Restore Dry Run agent mismatch")
    require(restore_dry.get("mode") == "dry_run_only", "Restore Dry Run mode must be dry_run_only")
    require(restore_dry.get("decision") == "allowed", "Restore Dry Run decision must be allowed")
    require(restore_dry.get("would_restore") is True, "Restore Dry Run would_restore must be true")
    require(restore_dry.get("requires_confirmation") is True, "Restore Dry Run requires_confirmation must be true")
    require(restore_dry.get("will_not_execute") is True, "Restore Dry Run will_not_execute must be true")
    assert_safety_false(
        restore_dry.get("safety") or {},
        ["restore", "restart", "docker_apply", "linux_deploy", "file_mutation", "runtime_mutation"],
        "Restore Dry Run",
    )
    print("✅ Restore Backup Dry Run response integrity correct")

    restore_missing = control("/__gateway_control/backup/restore/dry-run?backup=does-not-exist")
    require(restore_missing.get("ok") is False, "Missing Restore Dry Run ok must be false")
    require(restore_missing.get("decision") == "blocked", "Missing Restore Dry Run decision must be blocked")
    require(restore_missing.get("will_not_execute") is True, "Missing Restore Dry Run will_not_execute must be true")
    require(bool(restore_missing.get("reason")), "Missing Restore Dry Run reason must exist")
    print("✅ Missing Restore Dry Run response integrity correct")

    blocked_confirm = control(f"/__gateway_control/backup/restore/confirm?backup={quoted_backup}", timeout=30)
    require(blocked_confirm.get("ok") is False, "Confirm Restore without confirmation ok must be false")
    require(blocked_confirm.get("decision") == "blocked", "Confirm Restore without confirmation decision must be blocked")
    require(blocked_confirm.get("will_not_execute") is True, "Confirm Restore without confirmation will_not_execute must be true")
    require(bool(blocked_confirm.get("reason")), "Confirm Restore without confirmation reason must exist")
    print("✅ Confirmed Restore blocked response integrity correct")

    for action in ["disable", "drain", "enable"]:
        endpoint = (
            f"/__gateway_control/upstream/{action}/dry-run"
            f"?route=/api&upstream=127.0.0.1:9101"
            f"&runtime_context=local&address_mode=local"
        )
        result = control(endpoint)
        require(result.get("ok") is True, f"{action} dry-run ok must be true: {result}")
        require(result.get("agent") == "upstream_state_agent", f"{action} dry-run agent mismatch")
        require(result.get("mode") == "dry_run_only", f"{action} dry-run mode must be dry_run_only")
        require(result.get("action") == action, f"{action} dry-run action mismatch")
        require(result.get("route") == "/api", f"{action} dry-run route mismatch")
        require(result.get("upstream") == "127.0.0.1:9101", f"{action} dry-run upstream mismatch")
        require(result.get("will_not_execute") is True, f"{action} dry-run will_not_execute must be true")
        assert_local_context(result, f"{action} dry-run")
        assert_no_docker_leak(result, f"{action} dry-run")
        assert_safety_false(
            result.get("safety") or {},
            ["live_runtime_mutation", "docker_apply", "linux_deploy", "restart", "rollback"],
            f"{action} dry-run",
        )
        print(f"✅ Upstream {action.title()} Dry Run response integrity correct")

    runtime_after = get_json(f"{RUNTIME}/__gateway_runtime_state")
    require(runtime_after.get("ok") is True, "Runtime after endpoint checks must still be ok")
    assert_has_local_upstream(runtime_after, "runtime_after")
    assert_no_docker_leak(runtime_after, "runtime_after")
    print("✅ Runtime remained healthy after dry-run endpoint checks")

    print("✅ DASHBOARD ENDPOINT RESPONSE INTEGRITY VERIFIER PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
