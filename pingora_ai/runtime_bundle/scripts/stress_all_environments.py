#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run(cmd: list[str], *, timeout: int = 900, check: bool = True) -> subprocess.CompletedProcess:
    print("\n$ " + " ".join(cmd))
    result = subprocess.run(
        cmd,
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
    )

    if result.stdout:
        print(result.stdout)

    if result.stderr:
        print(result.stderr, file=sys.stderr)

    if check and result.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {result.returncode}: {' '.join(cmd)}")

    return result


def http_status(url: str, *, headers: dict[str, str] | None = None, insecure_tls: bool = False) -> tuple[int | None, str]:
    try:
        req = urllib.request.Request(url, headers=headers or {})
        context = ssl._create_unverified_context() if insecure_tls else None

        with urllib.request.urlopen(req, timeout=10, context=context) as response:
            body = response.read().decode(errors="ignore")
            return response.status, body

    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="ignore")
        return exc.code, body

    except Exception as exc:
        return None, str(exc)


def assert_status(url: str, expected: set[int], *, headers: dict[str, str] | None = None, insecure_tls: bool = False) -> str:
    status, body = http_status(url, headers=headers, insecure_tls=insecure_tls)
    print(f"{url} -> {status}")

    if status not in expected:
        raise AssertionError(f"{url} expected {expected}, got {status}. Body: {body[:300]}")

    return body


def ensure_fastapi() -> None:
    status, body = http_status("http://127.0.0.1:8000/health")

    if status != 200:
        raise RuntimeError(
            "FastAPI planner is not running on 127.0.0.1:8000.\n"
            "Start it in another terminal:\n\n"
            "cd ~/dev/pingora-generator\n"
            "source .venv/bin/activate\n"
            "python -m uvicorn server.main:app --host 127.0.0.1 --port 8000 --reload"
        )

    print("FastAPI planner health OK")


def start_vm_backend(host: str, user: str, ssh_key: str) -> None:
    run(
        [
            "ssh",
            "-i",
            ssh_key,
            f"{user}@{host}",
            """
nohup python3 /tmp/simple_backend_9101.py > /tmp/simple_backend_9101.log 2>&1 < /dev/null &
sleep 1
curl -fsS http://127.0.0.1:9101/health >/dev/null
echo backend_9101_ok
""",
        ],
        timeout=60,
    )


def clean_bare_metal(host: str, user: str, ssh_key: str) -> None:
    run(
        [
            "ssh",
            "-i",
            ssh_key,
            f"{user}@{host}",
            """
sudo pkill -9 -f "[p]ingora-gateway" || true
sudo systemctl kill -s SIGKILL pingora-ai-blue || true
sudo systemctl kill -s SIGKILL pingora-ai-green || true
sudo systemctl stop pingora-ai-blue pingora-ai-green || true
sudo systemctl reset-failed pingora-ai-blue pingora-ai-green || true
pkill -f "[s]imple_backend_9101.py" || true
echo bare_metal_cleaned
""",
        ],
        timeout=90,
        check=False,
    )


def local_cmd_base() -> list[str]:
    return ["pingora-ai"]


def docker_cmd_base() -> list[str]:
    return ["pingora-ai"]


def linux_flags(args: argparse.Namespace) -> list[str]:
    return [
        "--linux",
        "--host",
        args.host,
        "--user",
        args.user,
        "--ssh-key",
        args.ssh_key,
        "--binary-path",
        args.binary_path,
    ]




def assert_route_algorithm(path: str, expected: str) -> None:
    import json
    from pathlib import Path

    config_path = Path("generated-pingora-proxy/config.json")
    config = json.loads(config_path.read_text())

    routes = config.get("routes") or []
    route = next((r for r in routes if r.get("path") == path), None)

    if route is None:
        raise AssertionError(f"Route missing from config: {path}")

    if path == "/":
        actual = route.get("type") or route.get("route_type")
        if actual != "static":
            raise AssertionError(f"{path} expected static, got {actual}")
        print(f"{path} algorithm/static integrity -> OK")
        return

    actual = route.get("balancing") or route.get("algorithm") or "single"

    if actual != expected:
        raise AssertionError(f"{path} expected algorithm {expected}, got {actual}")

    print(f"{path} algorithm integrity -> {actual}")


def assert_route_missing(path: str) -> None:
    import json
    from pathlib import Path

    config = json.loads(Path("generated-pingora-proxy/config.json").read_text())
    routes = config.get("routes") or []

    if any(r.get("path") == path for r in routes):
        raise AssertionError(f"Route should have been removed but still exists: {path}")

    print(f"{path} removal integrity -> OK")


def test_local() -> None:
    print("\n==============================")
    print("LOCAL DIRECT STRESS")
    print("==============================")

    run(
        [
            "pingora-ai",
            "apply",
            "--local",
            "create gateway with /api to 127.0.0.1:9101",
        ]
    )

    assert_status("http://127.0.0.1:8090/", {200})
    assert_status("http://127.0.0.1:8090/api/", {200})
    assert_status("http://127.0.0.1:9101/health", {200})
    assert_status("http://127.0.0.1:8090/__gateway_runtime_state", {200, 404})
    assert_route_algorithm("/", "static")
    assert_route_algorithm("/api", "single")

    run(["pingora-ai", "add", "--local", "/coupons", "127.0.0.1:9401"])
    assert_status("http://127.0.0.1:8090/coupons/", {200})
    assert_status("http://127.0.0.1:9401/health", {200})
    assert_route_algorithm("/coupons", "single")

    run(["pingora-ai", "add", "--local", "/coupons", "127.0.0.1:9401"], check=False)

    run(
        [
            "pingora-ai",
            "edit",
            "--local",
            "/api",
            "--algorithm",
            "single",
            "--confirm-route-mutation",
        ]
    )
    assert_status("http://127.0.0.1:8090/api/", {200})
    assert_route_algorithm("/api", "single")

    run(
        [
            "pingora-ai",
            "remove",
            "--local",
            "/coupons",
            "--confirm-route-mutation",
        ]
    )

    assert_status("http://127.0.0.1:8090/", {200})
    assert_status("http://127.0.0.1:8090/api/", {200})
    assert_status("http://127.0.0.1:8090/.env", {403, 404})
    assert_route_missing("/coupons")
    assert_route_algorithm("/", "static")
    assert_route_algorithm("/api", "single")

    print("LOCAL DIRECT STRESS PASSED")


def test_docker() -> None:
    print("\n==============================")
    print("DOCKER BLUE/GREEN STRESS")
    print("==============================")

    run(
        [
            "pingora-ai",
            "apply",
            "--docker",
            "create gateway with /api random across 127.0.0.1:9101, 127.0.0.1:9102 and 127.0.0.1:9103",
        ]
    )

    assert_status("https://127.0.0.1:8443/", {200, 401}, insecure_tls=True)
    assert_status(
        "https://127.0.0.1:8443/api/",
        {200},
        headers={"X-API-Key": "dev-local-secret"},
        insecure_tls=True,
    )

    run(["pingora-ai", "add", "--docker", "/coupons", "127.0.0.1:9401"])
    assert_status(
        "https://127.0.0.1:8443/coupons/",
        {200},
        headers={"X-API-Key": "dev-local-secret"},
        insecure_tls=True,
    )

    run(["pingora-ai", "add", "--docker", "/coupons", "127.0.0.1:9401"], check=False)

    run(
        [
            "pingora-ai",
            "edit",
            "--docker",
            "/api",
            "--algorithm",
            "single",
            "--confirm-route-mutation",
        ]
    )

    assert_status(
        "https://127.0.0.1:8443/api/",
        {200},
        headers={"X-API-Key": "dev-local-secret"},
        insecure_tls=True,
    )

    run(
        [
            "pingora-ai",
            "remove",
            "--docker",
            "/coupons",
            "--confirm-route-mutation",
        ]
    )

    assert_status(
        "https://127.0.0.1:8443/api/",
        {200},
        headers={"X-API-Key": "dev-local-secret"},
        insecure_tls=True,
    )
    assert_status("https://127.0.0.1:8443/.env", {401, 403, 404}, insecure_tls=True)

    optional_scripts = [
        "scripts/verify_nginx_edge_security.py",
        "scripts/verify_tls_auth_secrets.py",
        "scripts/stress_nginx_edge_security.py",
    ]

    for script in optional_scripts:
        if (PROJECT_ROOT / script).exists():
            run([sys.executable, script], check=False)

    print("DOCKER BLUE/GREEN STRESS PASSED")


def test_bare_metal(args) -> None:
    print("\n==============================")
    print("BARE-METAL LINUX BLUE/GREEN STRESS")
    print("==============================")

    run(
        [
            "ssh",
            "-i",
            args.ssh_key,
            f"{args.user}@{args.host}",
            """
sudo pkill -9 -f "[p]ingora-gateway" || true
sudo systemctl kill -s SIGKILL pingora-ai-blue || true
sudo systemctl kill -s SIGKILL pingora-ai-green || true
sudo systemctl stop pingora-ai-blue pingora-ai-green || true
sudo systemctl reset-failed pingora-ai-blue pingora-ai-green || true
pkill -f "[s]imple_backend_9101.py" || true
echo bare_metal_cleaned
""",
        ]
    )

    run(
        [
            "ssh",
            "-i",
            args.ssh_key,
            f"{args.user}@{args.host}",
            """
nohup python3 /tmp/simple_backend_9101.py > /tmp/simple_backend_9101.log 2>&1 < /dev/null &
sleep 1
curl -fsS http://127.0.0.1:9101/health >/dev/null
echo backend_9101_ok
""",
        ]
    )

    run(
        [
            "pingora-ai",
            "apply",
            "--linux",
            "--host",
            args.host,
            "--user",
            args.user,
            "--ssh-key",
            args.ssh_key,
            "--binary-path",
            args.binary_path,
            "create gateway with /api to 127.0.0.1:9101",
        ]
    )

    assert_status(f"http://{args.host}/", {200})
    assert_status(f"http://{args.host}/api/", {200})
    assert_status(f"http://{args.host}/.env", {403, 404})
    assert_status(f"http://{args.host}/wp-admin", {403, 404})

    print("BARE-METAL LINUX APPLY/HEALTH STRESS PASSED")

def main() -> int:
    parser = argparse.ArgumentParser(description="Stress test local, Docker, and bare-metal Pingora AI environments.")
    parser.add_argument("--only", choices=["local", "docker", "linux", "all"], default="all")
    parser.add_argument("--host", default="192.168.252.2")
    parser.add_argument("--user", default="ubuntu")
    parser.add_argument("--ssh-key", default="./sshpingora_test")
    parser.add_argument(
        "--binary-path",
        default="generated-pingora-proxy/target/release/generated-pingora-proxy-linux",
    )

    args = parser.parse_args()

    ensure_fastapi()

    if args.only in {"local", "all"}:
        test_local()

    if args.only in {"docker", "all"}:
        test_docker()

    if args.only in {"linux", "all"}:
        test_bare_metal(args)

    print("\n===================================")
    print("ALL REQUESTED ENVIRONMENT STRESS TESTS PASSED")
    print("===================================")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
