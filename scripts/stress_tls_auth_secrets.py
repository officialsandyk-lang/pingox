from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
import ssl
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
PROMPT = "create gateway with /api random across 127.0.0.1:9101, 127.0.0.1:9102 and 127.0.0.1:9103"


def run(cmd: list[str], timeout: int = 1800):
    print("")
    print("$ " + " ".join(cmd))
    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT), text=True, capture_output=True, timeout=timeout)

    if result.stdout.strip():
        print(result.stdout[-12000:])
    if result.stderr.strip():
        print(result.stderr[-6000:])

    if result.returncode != 0:
        raise AssertionError(f"Command failed: {' '.join(cmd)}")
    return result


def https_ok() -> bool:
    ctx = ssl._create_unverified_context()
    api_key = os.getenv("PINGORA_EDGE_API_KEY", "dev-local-secret")
    req = urllib.request.Request("https://127.0.0.1:8443/api/")
    req.add_header("X-API-Key", api_key)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=5) as r:
            return int(r.status) == 200
    except Exception:
        return False


def wait_https():
    deadline = time.time() + 180
    while time.time() < deadline:
        if https_ok():
            print("✅ HTTPS /api is live")
            return
        time.sleep(2)
    raise AssertionError("Timed out waiting for HTTPS /api")


def main() -> int:
    print("🔐 TLS/Auth/Secrets Stress Test")

    os.environ.setdefault("PINGORA_EDGE_API_KEY", "dev-local-secret")

    run(["bash", "scripts/docker_ready.sh"], timeout=240)
    run(["bash", "scripts/generate_local_tls_cert.sh"], timeout=60)

    run([
        "pingora-ai",
        "apply",
        "--runtime",
        "docker_host",
        "--strategy",
        "bluegreen",
        "--verify",
        "full",
        PROMPT,
    ])

    wait_https()

    run([PYTHON, "scripts/verify_tls_auth_secrets.py"], timeout=240)

    print("")
    print("✅ TLS/AUTH/SECRETS STRESS TEST PASSED")
    print("✅ HTTPS works")
    print("✅ HTTP redirects")
    print("✅ missing/wrong API key rejected")
    print("✅ correct API key reaches /api")
    print("✅ secure headers/blocking/body limit still work")
    print("✅ Docker blue/green still works")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
