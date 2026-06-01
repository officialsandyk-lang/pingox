from __future__ import annotations

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


def http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return int(response.status) == 200
    except Exception:
        return False


def wait_api() -> None:
    deadline = time.time() + 180

    while time.time() < deadline:
        if http_ok("http://127.0.0.1:8090/api/"):
            print("✅ /api is live")
            return

        time.sleep(2)

    raise AssertionError("Timed out waiting for /api")


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
