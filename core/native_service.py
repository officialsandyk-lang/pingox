from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


SERVICE_NAME = "pingora-ai-runtime"


def _run(cmd: list[str], check: bool = False, timeout: int | None = None) -> dict[str, Any]:
    result = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    data = {
        "command": cmd,
        "returncode": result.returncode,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
    }
    if check and result.returncode != 0:
        raise RuntimeError(json.dumps(data, indent=2))
    return data


def port_owner(port: int) -> dict[str, Any]:
    return _run(["bash", "-lc", f"sudo ss -ltnp | grep ':{port} ' || true"])


def stop_nginx_if_requested(port: int, replace_edge: bool = False) -> dict[str, Any]:
    owner = port_owner(port)

    if "nginx" not in owner["stdout"]:
        return {
            "changed": False,
            "blocked": False,
            "message": f"Port {port} is not owned by nginx.",
            "owner": owner,
            "steps": [],
        }

    if not replace_edge:
        return {
            "changed": False,
            "blocked": True,
            "message": (
                f"Port {port} is owned by nginx. "
                f"Use --replace-edge if you want Pingora-AI to own port {port} directly."
            ),
            "owner": owner,
            "steps": [],
        }

    steps = [
        _run(["sudo", "systemctl", "stop", "nginx"], check=False),
        _run(["sudo", "systemctl", "disable", "nginx"], check=False),
    ]

    return {
        "changed": True,
        "blocked": False,
        "message": f"Nginx stopped/disabled so Pingora-AI can own port {port}.",
        "owner": owner,
        "steps": steps,
    }


def install_systemd_service(
    *,
    project_dir: Path,
    port: int,
    service_user: str = "ubuntu",
) -> dict[str, Any]:
    project_dir = project_dir.resolve()
    binary = project_dir / "target" / "release" / "generated-pingora-proxy"

    if not binary.exists():
        return {
            "success": False,
            "message": f"Generated binary not found: {binary}",
            "steps": [],
        }

    service_text = f"""[Unit]
Description=Pingora AI Runtime Gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={service_user}
WorkingDirectory={project_dir}
ExecStart={binary}
Restart=always
RestartSec=2
Environment=RUST_LOG=info

# Allow binding to privileged ports such as 80/443 without running as root.
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
"""

    tmp = Path("/tmp/pingora-ai-runtime.service")
    tmp.write_text(service_text, encoding="utf-8")

    steps = [
        _run(["sudo", "cp", str(tmp), f"/etc/systemd/system/{SERVICE_NAME}.service"], check=False),
        _run(["sudo", "systemctl", "daemon-reload"], check=False),
        _run(["sudo", "systemctl", "enable", SERVICE_NAME], check=False),
        _run(["sudo", "systemctl", "restart", SERVICE_NAME], check=False),
        _run(["sudo", "systemctl", "status", SERVICE_NAME, "--no-pager"], check=False),
        port_owner(port),
    ]

    active = _run(["systemctl", "is-active", SERVICE_NAME], check=False)

    return {
        "success": active["stdout"].strip() == "active",
        "message": f"Pingora-AI systemd service installed for port {port}.",
        "service": SERVICE_NAME,
        "project_dir": str(project_dir),
        "binary": str(binary),
        "active": active,
        "steps": steps,
    }


def stop_systemd_service() -> dict[str, Any]:
    steps = [
        _run(["sudo", "systemctl", "stop", SERVICE_NAME], check=False),
        _run(["sudo", "systemctl", "disable", SERVICE_NAME], check=False),
        _run(["sudo", "systemctl", "daemon-reload"], check=False),
    ]

    active = _run(["systemctl", "is-active", SERVICE_NAME], check=False)

    return {
        "success": active["stdout"].strip() != "active",
        "message": "Pingora-AI systemd service stopped/disabled.",
        "service": SERVICE_NAME,
        "active": active,
        "steps": steps,
    }
