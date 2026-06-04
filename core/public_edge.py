from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, asdict


@dataclass
class EdgeResult:
    success: bool
    message: str
    checks: list[dict]
    warnings: list[str]


def _run(command: list[str], check: bool = False) -> dict:
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
    )
    data = {
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
    }
    if check and result.returncode != 0:
        raise RuntimeError(json.dumps(data, indent=2))
    return data


def _has_command(name: str) -> bool:
    return shutil.which(name) is not None


def enable_public_edge() -> dict:
    """
    Prepare a Linux VM public HTTP/HTTPS edge.

    This is intentionally conservative:
    - installs/checks nginx only when apt exists
    - opens host iptables ports 80/443 before OCI Ubuntu reject rule
    - writes a simple public index page
    - verifies local HTTP
    - does not open cloud firewall/security-list automatically
    """
    checks: list[dict] = []
    warnings: list[str] = []

    if not _has_command("sudo"):
        return asdict(EdgeResult(
            success=False,
            message="sudo is required to enable public edge.",
            checks=checks,
            warnings=warnings,
        ))

    if _has_command("apt"):
        checks.append(_run(["sudo", "apt", "update"], check=False))
        checks.append(_run([
            "sudo", "apt", "install", "-y",
            "nginx", "curl", "iptables-persistent"
        ], check=False))
    else:
        warnings.append("apt not found; skipping package installation.")

    checks.append(_run([
        "bash",
        "-lc",
        "echo 'Pingora AI Public Edge OK' | sudo tee /var/www/html/index.html >/dev/null",
    ], check=False))

    # OCI Ubuntu images often have REJECT before later user-added accepts.
    # Insert accepts at the top so 80/443 are reached before reject.
    checks.append(_run(["sudo", "iptables", "-I", "INPUT", "1", "-p", "tcp", "--dport", "80", "-j", "ACCEPT"], check=False))
    checks.append(_run(["sudo", "iptables", "-I", "INPUT", "1", "-p", "tcp", "--dport", "443", "-j", "ACCEPT"], check=False))

    checks.append(_run(["sudo", "systemctl", "enable", "nginx"], check=False))
    checks.append(_run(["sudo", "nginx", "-t"], check=False))
    checks.append(_run(["sudo", "systemctl", "restart", "nginx"], check=False))

    if _has_command("netfilter-persistent"):
        checks.append(_run(["sudo", "netfilter-persistent", "save"], check=False))
    else:
        warnings.append("netfilter-persistent not found; iptables rules may not survive reboot.")

    local = _run(["curl", "-i", "--max-time", "5", "http://127.0.0.1/"], check=False)
    checks.append(local)

    success = local["returncode"] == 0 and "200 OK" in local["stdout"]

    if success:
        message = (
            "Public edge enabled locally. If public IP is not reachable, "
            "open OCI Security List/NSG ingress for TCP 80 and 443."
        )
    else:
        message = "Public edge setup ran, but local HTTP verification failed."

    return asdict(EdgeResult(
        success=success,
        message=message,
        checks=checks,
        warnings=warnings,
    ))


def main() -> int:
    result = enable_public_edge()
    print(json.dumps(result, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
