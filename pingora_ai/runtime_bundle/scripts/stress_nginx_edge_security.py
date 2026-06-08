from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable

PROMPT = "create gateway with /api random across 127.0.0.1:9101, 127.0.0.1:9102 and 127.0.0.1:9103"


def run(cmd: list[str], timeout: int = 1800):
    print("")
    print("$ " + " ".join(cmd))

    result = subprocess.run(
        cmd,
        cwd=str(PROJECT_ROOT),
        text=True,
        capture_output=True,
        timeout=timeout,
    )

    if result.stdout.strip():
        print(result.stdout[-12000:])

    if result.stderr.strip():
        print(result.stderr[-6000:])

    if result.returncode != 0:
        raise AssertionError(f"Command failed: {' '.join(cmd)}")

    return result


def http_ok(url: str, api_key: str | None = None) -> bool:
    try:
        request = urllib.request.Request(url)
        if api_key:
            request.add_header("X-API-Key", api_key)

        # Local dev uses a self-signed certificate, so use curl-style unverified SSL.
        if url.startswith("https://"):
            import ssl
            context = ssl._create_unverified_context()
            with urllib.request.urlopen(request, timeout=5, context=context) as response:
                return int(response.status) == 200

        with urllib.request.urlopen(request, timeout=5) as response:
            return int(response.status) == 200
    except Exception:
        return False


def wait_api() -> None:
    deadline = time.time() + 180
    api_key = os.getenv("PINGORA_EDGE_API_KEY", "dev-local-secret")

    while time.time() < deadline:
        if http_ok("https://127.0.0.1:8443/api/", api_key=api_key):
            print("✅ /api is live over HTTPS with API key")
            return

        time.sleep(2)

    raise AssertionError("Timed out waiting for /api over HTTPS with API key")


def main() -> int:
    print("🔐 NGINX Edge Security Stress Test")

    run(["bash", "scripts/docker_ready.sh"], timeout=240)

    run(
        [
            "pingora-ai",
            "apply",
            "--runtime",
            "docker_host",
            "--strategy",
            "bluegreen",
            "--verify",
            "full",
            PROMPT,
        ],
        timeout=1800,
    )

    wait_api()

    run([PYTHON, "scripts/verify_nginx_edge_security.py"], timeout=240)

    print("")
    print("✅ NGINX EDGE SECURITY STRESS TEST PASSED")
    print("✅ /api works")
    print("✅ blocked paths enforced")
    print("✅ secure headers present")
    print("✅ body-size limit enforced")
    print("✅ Docker blue/green still works")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
