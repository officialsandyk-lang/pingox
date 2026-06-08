from __future__ import annotations

import copy
import platform
import shutil
import subprocess
from typing import Any


def _run(cmd: list[str], timeout: int = 60) -> dict[str, Any]:
    p = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    return {
        "cmd": cmd,
        "returncode": p.returncode,
        "stdout": p.stdout[-3000:],
        "stderr": p.stderr[-3000:],
    }


def _ok(action: str, **extra: Any) -> dict[str, Any]:
    result = {"ok": True, "success": True, "action": action}
    result.update(extra)
    return result


def _not_repaired(action: str | None, reason: str, **extra: Any) -> dict[str, Any]:
    result = {
        "ok": False,
        "success": False,
        "action": action,
        "reason": reason,
    }
    result.update(extra)
    return result


def _ensure_iptables_accept_rule(port: int) -> dict[str, Any]:
    port_text = str(port)
    check = _run([
        "sudo",
        "iptables",
        "-C",
        "INPUT",
        "-p",
        "tcp",
        "--dport",
        port_text,
        "-j",
        "ACCEPT",
    ])

    if check["returncode"] == 0:
        check["changed"] = False
        check["description"] = f"iptables INPUT already allows tcp/{port_text}"
        return check

    insert = _run([
        "sudo",
        "iptables",
        "-I",
        "INPUT",
        "1",
        "-p",
        "tcp",
        "--dport",
        port_text,
        "-j",
        "ACCEPT",
    ])
    insert["changed"] = insert["returncode"] == 0
    insert["precheck"] = check
    insert["description"] = f"ensure iptables INPUT allows tcp/{port_text}"
    return insert


def _public_edge_preflight() -> dict[str, Any]:
    if platform.system().lower() != "linux":
        return _not_repaired(
            "repair_public_edge",
            "Public edge repair requires Linux with apt, iptables, systemd, and netfilter-persistent",
            platform=platform.system(),
        )

    required = ["sudo", "apt", "systemctl"]
    missing = [cmd for cmd in required if shutil.which(cmd) is None]
    if missing:
        return _not_repaired(
            "repair_public_edge",
            "Public edge repair requires missing host commands",
            missing_commands=missing,
            platform=platform.system(),
        )

    return _ok("repair_public_edge")


def repair_public_edge() -> dict[str, Any]:
    """
    Safe deterministic repair:
    - install/check nginx/curl/iptables-persistent
    - ensure 80/443 allow rules exist before reject
    - restart nginx
    - save iptables
    """
    preflight = _public_edge_preflight()
    if not preflight["ok"]:
        return preflight

    steps = []

    steps.append(_run(["sudo", "apt", "update"], timeout=120))
    steps.append(_run(["sudo", "apt", "install", "-y", "nginx", "curl", "iptables-persistent"], timeout=180))
    steps.append(_ensure_iptables_accept_rule(80))
    steps.append(_ensure_iptables_accept_rule(443))
    steps.append(_run(["sudo", "nginx", "-t"]))
    steps.append(_run(["sudo", "systemctl", "enable", "nginx"]))
    steps.append(_run(["sudo", "systemctl", "restart", "nginx"]))
    steps.append(_run(["sudo", "netfilter-persistent", "save"]))

    success = all(step["returncode"] == 0 for step in steps if step["cmd"][:2] != ["sudo", "apt"])
    return {"ok": success, "success": success, "action": "repair_public_edge", "steps": steps}


def add_default_blocked_paths(config: dict[str, Any]) -> dict[str, Any]:
    security = config.setdefault("security", {})
    blocked = security.setdefault("blocked_paths", [])

    defaults = ["/.env", "/.git", "/wp-admin", "/phpmyadmin", "/admin.php", "/wp-login.php"]
    for path in defaults:
        if path not in blocked:
            blocked.append(path)

    return config


def apply_safe_config_repairs(config: dict[str, Any], micro_result: dict[str, Any]) -> dict[str, Any]:
    """
    Applies only deterministic safe config repairs.
    Unsafe repairs, such as inventing backends, are never applied.
    """
    for decision in micro_result.get("failed_decisions", []) or []:
        action = decision.get("repair_action")
        safe = bool(decision.get("safe_to_auto_repair"))

        if not safe:
            continue

        if action == "add_default_blocked_paths":
            config = add_default_blocked_paths(config)

    return config


def _set_local_live_url(config: dict[str, Any]) -> dict[str, Any]:
    repaired = copy.deepcopy(config)
    live_url = "http://127.0.0.1:8090"
    changed: dict[str, dict[str, Any]] = {}

    for key in ("live_url", "public_url", "base_url"):
        if repaired.get(key) != live_url:
            changed[key] = {"from": repaired.get(key), "to": live_url}
            repaired[key] = live_url

    return _ok(
        "set_local_live_url_to_8090",
        changed=bool(changed),
        changes=changed,
        config=repaired,
        live_url=live_url,
    )


def _set_local_readiness_url(config: dict[str, Any], action: str) -> dict[str, Any]:
    repaired = copy.deepcopy(config)
    readiness_url = "http://127.0.0.1:8090/"
    changed = repaired.get("readiness_url") != readiness_url
    repaired["readiness_url"] = readiness_url

    return _ok(
        action,
        changed=changed,
        changes={"readiness_url": {"from": config.get("readiness_url"), "to": readiness_url}} if changed else {},
        config=repaired,
        readiness_url=readiness_url,
    )


def _replace_string_values(value: Any, old: str, new: str) -> tuple[Any, int]:
    if isinstance(value, dict):
        replaced: dict[Any, Any] = {}
        count = 0
        for key, child in value.items():
            new_child, child_count = _replace_string_values(child, old, new)
            replaced[key] = new_child
            count += child_count
        return replaced, count

    if isinstance(value, list):
        replaced_items = []
        count = 0
        for child in value:
            new_child, child_count = _replace_string_values(child, old, new)
            replaced_items.append(new_child)
            count += child_count
        return replaced_items, count

    if isinstance(value, str) and old in value:
        return value.replace(old, new), value.count(old)

    return value, 0


def _rewrite_docker_host_to_loopback(config: dict[str, Any], action: str) -> dict[str, Any]:
    repaired, replacements = _replace_string_values(
        copy.deepcopy(config),
        "host.docker.internal",
        "127.0.0.1",
    )

    return _ok(
        action,
        changed=replacements > 0,
        replacements=replacements,
        config=repaired,
    )


def repair_from_diagnosis(diagnosis: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Execute deterministic repairs selected by the incident commander.

    Repairs that require host, SSH, release, or service context fail closed
    instead of guessing from a root-cause label alone.
    """
    config = config or {}
    action = diagnosis.get("repair_action") or diagnosis.get("action")

    if not action:
        return _not_repaired(action, "Diagnosis did not include a repair_action")

    if "safe_to_auto_repair" in diagnosis and not bool(diagnosis.get("safe_to_auto_repair")):
        return _not_repaired(action, "Diagnosis is not marked safe_to_auto_repair")

    if action == "repair_public_edge":
        return repair_public_edge()

    if action == "add_default_blocked_paths":
        repaired = add_default_blocked_paths(copy.deepcopy(config))
        return _ok(action, changed=repaired != config, config=repaired)

    if action == "set_local_live_url_to_8090":
        return _set_local_live_url(config)

    if action in {
        "set_local_readiness_to_homepage",
        "replace_local_edge_health_wait_with_homepage_or_api",
    }:
        return _set_local_readiness_url(config, action)

    if action in {
        "rewrite_docker_host_to_loopback",
        "normalize_bare_metal_upstreams",
    }:
        return _rewrite_docker_host_to_loopback(config, action)

    if action in {
        "upload_static_assets_to_release",
        "upload_bare_metal_static_assets",
        "kill_process_then_reset_failed",
        "start_missing_demo_backends_for_config",
    }:
        return _not_repaired(
            action,
            "Repair requires deployment-specific host, release, service, or process context",
        )

    return _not_repaired(action, f"No deterministic repair registered for action: {action}")


def repair_by_action(action: str) -> dict[str, Any]:
    if action == "repair_public_edge":
        return repair_public_edge()

    return {
        "success": False,
        "action": action,
        "reason": f"No deterministic repair registered for action: {action}",
    }
