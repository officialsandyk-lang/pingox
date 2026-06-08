from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCKER_DIR = PROJECT_ROOT / "docker-bluegreen"


def _run(command: list[str], *, timeout: int = 120, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd or PROJECT_ROOT),
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def _print_result(title: str, result: subprocess.CompletedProcess[str]) -> None:
    print("")
    print(title)
    print(f"Return code: {result.returncode}")

    if result.stdout.strip():
        print("STDOUT:")
        print(result.stdout.strip()[-4000:])

    if result.stderr.strip():
        print("STDERR:")
        print(result.stderr.strip()[-4000:])


def stop_local() -> int:
    """
    Stop local/direct Pingora runtime.

    Local policy: local dev/test uses port 8080.
    Also best-effort stop legacy ports 8090/8443 only if present.
    """
    ports = [8080, 8090, 8443]

    print("🛑 Stopping local Pingora runtime")
    for port in ports:
        result = _run(
            ["bash", "-lc", f"lsof -tiTCP:{port} -sTCP:LISTEN | xargs -r kill -9"],
            timeout=30,
        )
        if result.returncode == 0:
            print(f"✅ Stopped listeners on port {port} if any.")
        else:
            print(f"⚠️ Could not stop port {port}: {result.stderr.strip()}")

    return 0


def status_local() -> int:
    print("📊 Local runtime status")
    result = _run(
        [
            "bash",
            "-lc",
            "for p in 8080 8090 8443; do "
            "echo PORT=$p; "
            "lsof -iTCP:$p -sTCP:LISTEN -n -P || true; "
            "done",
        ],
        timeout=30,
    )
    _print_result("Local listeners", result)

    health = _run(
        ["bash", "-lc", "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/ || true"],
        timeout=10,
    )
    print(f"Local 8080 HTTP status: {(health.stdout or '').strip() or 'unreachable'}")
    return 0


def stop_docker() -> int:
    print("🛑 Stopping Docker blue/green gateway")

    if (DOCKER_DIR / "docker-compose.yml").exists():
        result = _run(
            ["docker", "compose", "-f", str(DOCKER_DIR / "docker-compose.yml"), "down", "--remove-orphans"],
            timeout=300,
        )
        _print_result("docker compose down", result)

    rm = _run(
        [
            "bash",
            "-lc",
            "docker rm -f ai-pingora-gateway-blue ai-pingora-gateway-green ai-pingora-gateway-edge 2>/dev/null || true",
        ],
        timeout=120,
    )
    _print_result("remove known containers", rm)

    return 0


def status_docker() -> int:
    print("📊 Docker blue/green status")

    ps = _run(
        [
            "bash",
            "-lc",
            "docker ps -a --filter 'name=ai-pingora-gateway' "
            "--format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'",
        ],
        timeout=60,
    )
    _print_result("Docker containers", ps)

    checks = {
        "root": "http://127.0.0.1:80/",
        "api": "http://127.0.0.1:80/api/",
        "runtime_state": "http://127.0.0.1:80/__gateway_runtime_state",
    }

    for name, url in checks.items():
        result = _run(
            ["bash", "-lc", f"curl -s -o /dev/null -w '%{{http_code}}' {url} || true"],
            timeout=10,
        )
        print(f"Docker {name} HTTP status: {(result.stdout or '').strip() or 'unreachable'}")

    return 0


def _ssh_base(args: Any) -> list[str]:
    if not getattr(args, "host", None):
        raise SystemExit("--host is required for --linux status/stop.")

    user = getattr(args, "user", None) or "ubuntu"
    ssh_port = str(getattr(args, "ssh_port", 22) or 22)

    command = ["ssh", "-p", ssh_port]

    ssh_key = getattr(args, "ssh_key", None)
    if ssh_key:
        command.extend(["-i", str(Path(ssh_key).expanduser())])

    command.extend(
        [
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ServerAliveInterval=10",
            "-o",
            "ServerAliveCountMax=3",
            f"{user}@{args.host}",
        ]
    )

    return command


def _ssh(args: Any, remote_command: str, *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return _run(_ssh_base(args) + [remote_command], timeout=timeout)


def stop_linux(args: Any) -> int:
    print("🛑 Stopping Linux/systemd Pingora gateway")
    print(f"Host: {args.host}")
    print(f"User: {getattr(args, 'user', 'ubuntu')}")

    remote = r'''
set -e
sudo systemctl stop pingora-ai-blue pingora-ai-green 2>/dev/null || true
sudo systemctl disable pingora-ai-blue pingora-ai-green 2>/dev/null || true
sudo systemctl reset-failed pingora-ai-blue pingora-ai-green 2>/dev/null || true
sudo pkill -9 -f '/opt/pingora-ai/releases/.*/pingora-gateway' 2>/dev/null || true
echo STOPPED
sudo lsof -iTCP:80 -sTCP:LISTEN -n -P || true
'''
    result = _ssh(args, remote, timeout=180)
    _print_result("Linux stop", result)
    return 0 if result.returncode == 0 else result.returncode


def status_linux(args: Any) -> int:
    print("📊 Linux/systemd Pingora gateway status")
    print(f"Host: {args.host}")
    print(f"User: {getattr(args, 'user', 'ubuntu')}")

    remote = r'''
echo "== services =="
sudo systemctl is-active pingora-ai-blue pingora-ai-green nginx 2>/dev/null || true

echo
echo "== pingora blue =="
sudo systemctl status pingora-ai-blue --no-pager -l 2>/dev/null | head -80 || true

echo
echo "== pingora green =="
sudo systemctl status pingora-ai-green --no-pager -l 2>/dev/null | head -80 || true

echo
echo "== nginx =="
sudo systemctl status nginx --no-pager -l 2>/dev/null | head -60 || true

echo
echo "== port 80 listener =="
sudo lsof -iTCP:80 -sTCP:LISTEN -n -P || true

echo
echo "== public checks =="
curl -s -o /dev/null -w 'root=%{http_code}\n' http://127.0.0.1/ || true
curl -s -o /dev/null -w 'api=%{http_code}\n' http://127.0.0.1/api/ || true
curl -s -o /dev/null -w 'runtime_state=%{http_code}\n' http://127.0.0.1/__gateway_runtime_state || true
'''
    result = _ssh(args, remote, timeout=180)
    _print_result("Linux status", result)
    return 0 if result.returncode == 0 else result.returncode


def wait_for_docker_public_ready(timeout_seconds: int = 90) -> bool:
    import time

    checks = {
        "root": "http://127.0.0.1:80/",
        "api": "http://127.0.0.1:80/api/",
        "runtime_state": "http://127.0.0.1:80/__gateway_runtime_state",
    }

    deadline = time.time() + timeout_seconds
    last = {}

    while time.time() < deadline:
        ok = True
        last = {}

        for name, url in checks.items():
            result = _run(
                ["bash", "-lc", f"curl -s -o /dev/null -w '%{{http_code}}' {url} || true"],
                timeout=10,
            )
            status = (result.stdout or "").strip()
            last[name] = status
            if status != "200":
                ok = False

        if ok:
            print("✅ Docker public edge ready after lifecycle operation.")
            return True

        time.sleep(2)

    print(f"❌ Docker public edge did not become fully ready. Last statuses: {last}")
    return False


# ---------------------------------------------------------------------
# Docker lifecycle controls
# Safe: does not delete configs, backups, or generated files.
# ---------------------------------------------------------------------

def start_docker() -> int:
    print("▶️ Starting Docker blue/green gateway")

    compose_file = DOCKER_DIR / "docker-compose.yml"
    if not compose_file.exists():
        print("❌ docker-bluegreen/docker-compose.yml not found. Run pingora-ai apply --docker first.")
        return 2

    result = _run(
        ["docker", "compose", "-f", str(compose_file), "up", "-d"],
        timeout=300,
    )
    _print_result("docker compose up", result)

    if result.returncode != 0:
        return result.returncode

    if not wait_for_docker_public_ready(timeout_seconds=90):
        return 1

    return status_docker()


def restart_docker() -> int:
    print("🔁 Restarting Docker blue/green gateway")

    compose_file = DOCKER_DIR / "docker-compose.yml"
    if not compose_file.exists():
        print("❌ docker-bluegreen/docker-compose.yml not found. Run pingora-ai apply --docker first.")
        return 2

    result = _run(
        ["docker", "compose", "-f", str(compose_file), "restart"],
        timeout=300,
    )
    _print_result("docker compose restart", result)

    if result.returncode != 0:
        return result.returncode

    if not wait_for_docker_public_ready(timeout_seconds=90):
        return 1

    return status_docker()


def reload_docker() -> int:
    """
    Docker Pingora reload is currently implemented as a safe restart.

    This does not remove containers, configs, generated files, or backups.
    Later this can become true hot reload when runtime support is added.
    """
    print("🔄 Reloading Docker gateway")
    return restart_docker()
