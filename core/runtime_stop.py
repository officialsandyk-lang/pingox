from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class StopResult:
    success: bool
    message: str
    stopped: list[dict[str, Any]]
    warnings: list[str]


def _run(cmd: list[str], check: bool = False) -> dict[str, Any]:
    result = subprocess.run(cmd, text=True, capture_output=True)
    data = {
        "command": cmd,
        "returncode": result.returncode,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
    }
    if check and result.returncode != 0:
        raise RuntimeError(json.dumps(data, indent=2))
    return data


def _kill_by_pattern(pattern: str, label: str) -> dict[str, Any]:
    before = _run(["bash", "-lc", f"pgrep -af {json.dumps(pattern)} || true"])
    _run(["bash", "-lc", f"pkill -f {json.dumps(pattern)} || true"])
    after = _run(["bash", "-lc", f"pgrep -af {json.dumps(pattern)} || true"])

    return {
        "label": label,
        "pattern": pattern,
        "before": before["stdout"].strip(),
        "after": after["stdout"].strip(),
        "stopped": bool(before["stdout"].strip()) and not bool(after["stdout"].strip()),
    }


def _port_check(port: int) -> dict[str, Any]:
    return _run(["bash", "-lc", f"ss -ltnp | grep ':{port} ' || true"])


def stop_runtime(
    *,
    stop_edge: bool = False,
    stop_systemd: bool = False,
) -> dict[str, Any]:
    stopped: list[dict[str, Any]] = []
    warnings: list[str] = []

    if stop_systemd:
        try:
            from core.native_service import stop_systemd_service
            stopped.append({"label": "systemd_service", "result": stop_systemd_service()})
        except Exception as exc:
            warnings.append(f"Failed to stop systemd service: {exc}")

    patterns = [
        ("generated-pingora-proxy", "pingora_generated_runtime"),
        ("cargo run --release", "cargo_runtime_runner"),
        ("main.py --runtime local", "local_apply_process"),
        ("update.py --runtime local", "local_update_process"),
        ("scripts/demo_backends.py serve", "demo_backends"),
    ]

    for pattern, label in patterns:
        stopped.append(_kill_by_pattern(pattern, label))

    if stop_edge:
        stopped.append({
            "label": "nginx_public_edge",
            "result": _run(["sudo", "systemctl", "stop", "nginx"], check=False),
        })
    else:
        warnings.append("Public edge/Nginx left running. Use --edge to stop it too.")

    ports = {
        "80": _port_check(80),
        "443": _port_check(443),
        "8090": _port_check(8090),
        "9101": _port_check(9101),
        "9401": _port_check(9401),
        "9402": _port_check(9402),
    }

    still_open = {
        port: data["stdout"].strip()
        for port, data in ports.items()
        if data["stdout"].strip()
    }

    # If user did not ask to stop edge, port 80/443 may remain open by design.
    ignored = set()
    if not stop_edge:
        ignored.update({"80", "443"})

    effective_open = {
        port: value for port, value in still_open.items()
        if port not in ignored
    }

    success = not bool(effective_open)

    if success:
        message = "Pingora-AI stopped successfully. Runtime processes stopped and requested ports are released."
    else:
        message = "Stop ran, but some runtime ports are still open."

    return asdict(StopResult(
        success=success,
        message=message,
        stopped=stopped + [{"label": "port_checks", "ports": ports, "still_open": still_open}],
        warnings=warnings,
    ))
