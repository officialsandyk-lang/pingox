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


def connect_public_edge(target: str = "http://127.0.0.1:8090") -> dict:
    """
    Connect the host public HTTP edge to the local Pingora runtime.

    This is for user-mode Linux/OCI installs:
      public :80 -> nginx -> local Pingora runtime target, usually 127.0.0.1:8090

    The user should not manually write nginx config. The CLI owns this.
    """
    checks: list[dict] = []
    warnings: list[str] = []

    target = str(target or "http://127.0.0.1:8090").strip().rstrip("/")

    if not target.startswith(("http://127.0.0.1:", "http://localhost:")):
        return asdict(EdgeResult(
            success=False,
            message=(
                "Refusing to connect public edge to a non-local target. "
                "Use a local target such as http://127.0.0.1:8090."
            ),
            checks=checks,
            warnings=warnings,
        ))

    if not _has_command("sudo"):
        return asdict(EdgeResult(
            success=False,
            message="sudo is required to connect public edge.",
            checks=checks,
            warnings=warnings,
        ))

    if not _has_command("nginx"):
        if _has_command("apt"):
            checks.append(_run(["sudo", "apt", "update"], check=False))
            checks.append(_run(["sudo", "apt", "install", "-y", "nginx", "curl"], check=False))
        else:
            return asdict(EdgeResult(
                success=False,
                message="nginx is required and apt was not found for installation.",
                checks=checks,
                warnings=warnings,
            ))

    nginx_conf = f"""
server {{
    listen 80 default_server;
    listen [::]:80 default_server;

    server_name _;

    server_tokens off;
    client_max_body_size 10m;

    location = /.env {{ return 403; }}
    location ^~ /.git {{ return 403; }}
    location ^~ /wp-admin {{ return 403; }}
    location ^~ /phpmyadmin {{ return 403; }}
    location = /admin.php {{ return 403; }}
    location ~* \\.(php|phtml|sql|bak|old|swp|ini|env|log)$ {{ return 403; }}

    location / {{
        proxy_pass {target};
        proxy_http_version 1.1;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_read_timeout 60s;
        proxy_connect_timeout 10s;
        proxy_send_timeout 60s;
    }}
}}
""".strip()

    tmp_path = "/tmp/pingora-ai-edge.conf"

    checks.append(_run([
        "bash",
        "-lc",
        f"cat > {tmp_path} <<'EOF'\n{nginx_conf}\nEOF",
    ], check=False))

    checks.append(_run([
        "sudo", "cp", tmp_path, "/etc/nginx/sites-available/pingora-ai-edge"
    ], check=False))

    checks.append(_run([
        "sudo", "rm", "-f",
        "/etc/nginx/sites-enabled/default",
        "/etc/nginx/sites-available/default",
    ], check=False))

    checks.append(_run([
        "sudo", "ln", "-sf",
        "/etc/nginx/sites-available/pingora-ai-edge",
        "/etc/nginx/sites-enabled/pingora-ai-edge",
    ], check=False))

    # Keep OCI iptables allow rules before reject.
    checks.append(_run(["sudo", "iptables", "-I", "INPUT", "1", "-p", "tcp", "--dport", "80", "-j", "ACCEPT"], check=False))
    checks.append(_run(["sudo", "iptables", "-I", "INPUT", "1", "-p", "tcp", "--dport", "443", "-j", "ACCEPT"], check=False))

    checks.append(_run(["sudo", "nginx", "-t"], check=False))
    if checks[-1]["returncode"] != 0:
        return asdict(EdgeResult(
            success=False,
            message="Nginx config test failed. Public edge was not reloaded.",
            checks=checks,
            warnings=warnings,
        ))

    checks.append(_run(["sudo", "systemctl", "enable", "nginx"], check=False))
    checks.append(_run(["sudo", "systemctl", "reload", "nginx"], check=False))

    if _has_command("netfilter-persistent"):
        checks.append(_run(["sudo", "netfilter-persistent", "save"], check=False))
    else:
        warnings.append("netfilter-persistent not found; iptables rules may not survive reboot.")

    home = _run(["curl", "-i", "--max-time", "5", "http://127.0.0.1/"], check=False)
    api = _run(["curl", "-i", "--max-time", "5", "http://127.0.0.1/api/"], check=False)
    blocked = _run(["curl", "-i", "--max-time", "5", "http://127.0.0.1/.env"], check=False)

    checks.extend([home, api, blocked])

    success = (
        home["returncode"] == 0
        and "200 OK" in home["stdout"]
        and api["returncode"] == 0
        and "200 OK" in api["stdout"]
        and "403" in blocked["stdout"]
    )

    message = (
        f"Public edge connected to {target}. "
        "If public IP is not reachable, check OCI Security List/NSG ingress for TCP 80."
        if success
        else f"Public edge config applied but verification failed for target {target}."
    )

    return asdict(EdgeResult(
        success=success,
        message=message,
        checks=checks,
        warnings=warnings,
    ))
