from __future__ import annotations

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


def repair_public_edge() -> dict[str, Any]:
    """
    Safe deterministic repair:
    - install/check nginx/curl/iptables-persistent
    - put 80/443 allow rules before reject
    - restart nginx
    - save iptables
    """
    steps = []

    steps.append(_run(["sudo", "apt", "update"], timeout=120))
    steps.append(_run(["sudo", "apt", "install", "-y", "nginx", "curl", "iptables-persistent"], timeout=180))
    steps.append(_run(["sudo", "iptables", "-I", "INPUT", "1", "-p", "tcp", "--dport", "80", "-j", "ACCEPT"]))
    steps.append(_run(["sudo", "iptables", "-I", "INPUT", "1", "-p", "tcp", "--dport", "443", "-j", "ACCEPT"]))
    steps.append(_run(["sudo", "nginx", "-t"]))
    steps.append(_run(["sudo", "systemctl", "enable", "nginx"]))
    steps.append(_run(["sudo", "systemctl", "restart", "nginx"]))
    steps.append(_run(["sudo", "netfilter-persistent", "save"]))

    success = all(step["returncode"] == 0 for step in steps if step["cmd"][:2] != ["sudo", "apt"])
    return {"success": success, "action": "repair_public_edge", "steps": steps}


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


def repair_by_action(action: str) -> dict[str, Any]:
    if action == "repair_public_edge":
        return repair_public_edge()

    return {
        "success": False,
        "action": action,
        "reason": f"No deterministic repair registered for action: {action}",
    }
