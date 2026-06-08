from __future__ import annotations

import json
import re
import urllib.request
from typing import Any


CONTROL = "http://127.0.0.1:8787"
RUNTIME = "http://127.0.0.1:8080"
TOKEN = "dev-local-control-token"


def get_json(url: str, token: bool = False) -> dict[str, Any]:
    headers = {"X-Pingora-Control-Token": TOKEN} if token else {}
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=10) as res:
        return json.loads(res.read().decode("utf-8"))


def post_json(url: str) -> dict[str, Any]:
    req = urllib.request.Request(url, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=10) as res:
        return json.loads(res.read().decode("utf-8"))


def get_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=10) as res:
        return res.read().decode("utf-8", errors="replace")


def require(ok: bool, message: str) -> None:
    if not ok:
        raise SystemExit(f"❌ {message}")


def backend_counts(n: int = 20) -> dict[str, int]:
    counts: dict[str, int] = {}
    for _ in range(n):
        body = get_text(f"{RUNTIME}/api/")
        found = re.findall(r'"backend_port"\s*:\s*(\d+)', body)
        if not found:
            found = re.findall(r"Demo Backend\s+(\d+)", body)
        for port in found:
            counts[port] = counts.get(port, 0) + 1
    return counts


def control(path: str) -> dict[str, Any]:
    return get_json(f"{CONTROL}{path}", token=True)


def main() -> int:
    print("🧪 Verifying dashboard button outputs for local runtime")

    runtime_state = get_json(f"{RUNTIME}/__gateway_runtime_state")
    require(runtime_state.get("ok") is True, "Fetch Runtime State endpoint failed")
    runtime_text = json.dumps(runtime_state)
    require("127.0.0.1:9101" in runtime_text, "runtime_state missing 127.0.0.1:9101")
    require("host.docker.internal" not in runtime_text, "runtime_state leaked host.docker.internal")
    print("✅ Fetch Runtime State output correct")

    status = control("/__gateway_control/status")
    require(status.get("runtime_context") == "local", "Local Status runtime_context not local")
    require(status.get("address_mode") == "local", "Local Status address_mode not local")
    require(status.get("runtime") == "local", "Local Status runtime not local")
    require(status.get("strategy") == "direct", "Local Status strategy not direct")
    require(status.get("public_checks", {}).get("root") == "200", "Local Status root not 200")
    require(status.get("public_checks", {}).get("api") == "200", "Local Status api not 200")
    require(status.get("public_checks", {}).get("runtime_state") == "200", "Local Status runtime_state not 200")
    print("✅ Local Status output correct")

    upstreams = control("/__gateway_control/upstreams")
    upstreams_text = json.dumps(upstreams)
    require(upstreams.get("runtime_context") == "local", "Upstream List runtime_context not local")
    require(upstreams.get("address_mode") == "local", "Upstream List address_mode not local")
    require("127.0.0.1:9101" in upstreams_text, "Upstream List missing local 9101")
    require("127.0.0.1:9102" in upstreams_text, "Upstream List missing local 9102")
    require("host.docker.internal" not in upstreams_text, "Upstream List leaked host.docker.internal")
    print("✅ Upstream List output correct")

    doctor = control("/__gateway_control/doctor")
    require(doctor.get("ok") is True, "Doctor Report not ok")
    require(doctor.get("runtime_context") == "local", "Doctor runtime_context not local")
    require(doctor.get("address_mode") == "local", "Doctor address_mode not local")
    require(doctor.get("checked_base_url") == RUNTIME, "Doctor checked wrong base URL")
    require(doctor.get("checks", {}).get("root", {}).get("status") == 200, "Doctor root status not 200")
    require(doctor.get("checks", {}).get("api", {}).get("status") == 200, "Doctor api status not 200")
    require(doctor.get("checks", {}).get("runtime_state", {}).get("status") == 200, "Doctor runtime_state status not 200")
    print("✅ Doctor Report output correct")

    dashboard_check = control("/__gateway_control/dashboard-check")
    require(dashboard_check.get("ok") is True, "Dashboard Check not ok")
    require(dashboard_check.get("runtime_context") == "local", "Dashboard Check runtime_context not local")
    require(dashboard_check.get("address_mode") == "local", "Dashboard Check address_mode not local")
    require(dashboard_check.get("checked_base_url") == RUNTIME, "Dashboard Check checked wrong base URL")
    require(dashboard_check.get("dashboard_status") == 200, "Dashboard Check dashboard_status not 200")

    markers = dashboard_check.get("dashboard_markers") or {}
    missing = [name for name, ok in markers.items() if not ok]
    require(not missing, f"Dashboard Check missing markers: {missing}")

    print("✅ Dashboard Check output correct")

    backups = control("/__gateway_control/backups")
    require(backups.get("ok") is True, "Backup List not ok")
    require(backups.get("policy") == "Backups are only for valid deployment attempts.", "Backup policy text changed")

    backup_items = backups.get("items") or []
    require(bool(backup_items), "No backups available for restore dry-run test")

    restore_backup_name = str(backup_items[0].get("name") or "")
    require(bool(restore_backup_name), "Latest backup item has no name")

    print("✅ Backup List output correct")

    restore_dry_run = control(f"/__gateway_control/backup/restore/dry-run?backup={restore_backup_name}")
    require(restore_dry_run.get("ok") is True, "Restore Backup Dry Run not ok")
    require(restore_dry_run.get("agent") == "backup_restore_agent", "Restore Backup Dry Run wrong agent")
    require(restore_dry_run.get("mode") == "dry_run_only", "Restore Backup Dry Run mode not dry_run_only")
    require(restore_dry_run.get("decision") == "allowed", "Restore Backup Dry Run decision not allowed")
    require(restore_dry_run.get("would_restore") is True, "Restore Backup Dry Run would_restore not true")
    require(restore_dry_run.get("requires_confirmation") is True, "Restore Backup Dry Run should require confirmation")
    require(restore_dry_run.get("will_not_execute") is True, "Restore Backup Dry Run should not execute")

    restore_safety = restore_dry_run.get("safety") or {}
    require(restore_safety.get("restore") is False, "Restore Backup Dry Run safety.restore should be false")
    require(restore_safety.get("file_mutation") is False, "Restore Backup Dry Run safety.file_mutation should be false")
    require(restore_safety.get("runtime_mutation") is False, "Restore Backup Dry Run safety.runtime_mutation should be false")
    require(restore_safety.get("restart") is False, "Restore Backup Dry Run safety.restart should be false")
    require(restore_safety.get("docker_apply") is False, "Restore Backup Dry Run safety.docker_apply should be false")
    require(restore_safety.get("linux_deploy") is False, "Restore Backup Dry Run safety.linux_deploy should be false")

    invalid_restore_dry_run = control("/__gateway_control/backup/restore/dry-run?backup=does-not-exist")
    require(invalid_restore_dry_run.get("ok") is False, "Invalid Restore Backup Dry Run should not be ok")
    require(invalid_restore_dry_run.get("decision") == "blocked", "Invalid Restore Backup Dry Run should be blocked")
    require(invalid_restore_dry_run.get("backup_exists") is False, "Invalid Restore Backup Dry Run backup_exists should be false")
    require(invalid_restore_dry_run.get("will_not_execute") is True, "Invalid Restore Backup Dry Run should not execute")

    print("✅ Restore Backup Dry Run output correct")

    performance = control("/__gateway_control/performance?requests=20")
    require(performance.get("ok") is True, "Performance Report not ok")
    require(performance.get("environment") == "local", "Performance environment not local")
    require(performance.get("runtime_context") == "local", "Performance runtime_context not local")
    require(performance.get("address_mode") == "local", "Performance address_mode not local")
    require(performance.get("success_rate") == 100.0, "Performance success_rate not 100")
    require(bool(performance.get("backend_distribution")), "Performance backend_distribution empty")
    print("✅ Performance Report output correct")

    for action in ["disable", "drain", "enable"]:
        report = control(f"/__gateway_control/upstream/{action}/dry-run?route=/api&upstream=host.docker.internal:9101")
        text = json.dumps(report)
        require(report.get("agent") == "upstream_state_agent", f"{action} dry-run wrong agent")
        require(report.get("mode") == "dry_run_only", f"{action} dry-run wrong mode")
        require(report.get("runtime_context") == "local", f"{action} dry-run runtime_context not local")
        require(report.get("address_mode") == "local", f"{action} dry-run address_mode not local")
        require(report.get("will_not_execute") is True, f"{action} dry-run should not execute")
        require(report.get("live_runtime_mutation") in (False, None), f"{action} dry-run should not live mutate")
        require("127.0.0.1:9101" in text, f"{action} dry-run missing local 9101")
        require("host.docker.internal" not in text, f"{action} dry-run leaked host.docker.internal")
        print(f"✅ Dry-run {action} output correct")

    live_disable = control("/__gateway_control/runtime-admin/upstream/disable?route=/api&upstream=127.0.0.1:9101&confirm=true")
    require(live_disable.get("agent") == "runtime_admin_agent", "Live Disable wrong agent")
    require(live_disable.get("live_runtime_mutation") is True, "Live Disable did not mutate runtime")
    require(live_disable.get("docker_apply") is False, "Live Disable should not docker apply")
    require(live_disable.get("restart") is False, "Live Disable should not restart")
    counts_disabled = backend_counts(20)
    require(counts_disabled.get("9101", 0) == 0, "Live Disable still sent traffic to 9101")
    require(counts_disabled.get("9102", 0) > 0, "Live Disable did not send traffic to 9102")
    print("✅ Live Disable output and traffic correct")

    live_enable = control("/__gateway_control/runtime-admin/upstream/enable?route=/api&upstream=127.0.0.1:9101&confirm=true")
    require(live_enable.get("live_runtime_mutation") is True, "Live Enable did not mutate runtime")
    counts_enabled = backend_counts(20)
    require(counts_enabled.get("9101", 0) > 0, "Live Enable did not restore 9101 traffic")
    require(counts_enabled.get("9102", 0) > 0, "Live Enable missing 9102 traffic")
    print("✅ Live Enable output and traffic correct")

    live_drain = control("/__gateway_control/runtime-admin/upstream/drain?route=/api&upstream=127.0.0.1:9101&confirm=true")
    require(live_drain.get("live_runtime_mutation") is True, "Live Drain did not mutate runtime")
    counts_drained = backend_counts(20)
    require(counts_drained.get("9101", 0) == 0, "Live Drain still sent traffic to 9101")
    require(counts_drained.get("9102", 0) > 0, "Live Drain did not send traffic to 9102")
    print("✅ Live Drain output and traffic correct")

    restore = control("/__gateway_control/runtime-admin/upstream/enable?route=/api&upstream=127.0.0.1:9101&confirm=true")
    require(restore.get("live_runtime_mutation") is True, "Final restore enable failed")

    print("✅ DASHBOARD BUTTON OUTPUT VERIFIER PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
