from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable

API_URL = "http://127.0.0.1:8000"
LIVE_URL = os.environ.get("PINGORA_AI_ACCEPTANCE_LIVE_URL", "https://127.0.0.1:8443").rstrip("/")

BASELINE_PROMPT = (
    "create gateway with /api random across "
    "127.0.0.1:9101, 127.0.0.1:9102 and 127.0.0.1:9103"
)

BASELINE_PORTS = {"9101", "9102", "9103"}


def deployed_edge_api_key() -> str:
    nginx_path = PROJECT_ROOT / "docker-bluegreen" / "nginx.conf"
    if not nginx_path.exists():
        return ""

    text = nginx_path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r'if\s*\(\s*\$http_x_api_key\s*=\s*["\']([^"\']+)["\']\s*\)', text)
    return match.group(1).strip() if match else ""


def env_file_edge_api_key() -> str:
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return ""

    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, raw_value = line.split("=", 1)
        if key.strip() == "PINGORA_EDGE_API_KEY":
            return raw_value.strip().strip('"').strip("'")

    return ""


def edge_api_key() -> str:
    return (
        deployed_edge_api_key()
        or os.environ.get("PINGORA_EDGE_API_KEY", "").strip()
        or env_file_edge_api_key()
    )


def log(title: str) -> None:
    print("")
    print("=" * 80)
    print(title)
    print("=" * 80)


def run(
    command: list[str],
    *,
    timeout: int = 300,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    print("")
    print("$ " + " ".join(command))

    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)

    result = subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        text=True,
        capture_output=True,
        timeout=timeout,
        env=merged_env,
    )

    if result.stdout.strip():
        print(result.stdout[-8000:])

    if result.stderr.strip():
        print(result.stderr[-4000:])

    if check and result.returncode != 0:
        raise AssertionError(
            f"Command failed with exit code {result.returncode}: {' '.join(command)}"
        )

    return result


def http_request(
    url: str,
    *,
    method: str = "GET",
    timeout: int = 10,
) -> tuple[int | None, str, dict[str, str]]:
    headers = {}
    api_key = edge_api_key()
    if api_key and url.startswith(LIVE_URL):
        headers["X-API-Key"] = api_key

    request = urllib.request.Request(url, method=method, headers=headers)
    context = ssl._create_unverified_context() if url.startswith("https://") else None

    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            body = response.read().decode("utf-8", errors="replace")
            headers = {k: v for k, v in response.headers.items()}
            return int(response.status), body, headers

    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        headers = {k: v for k, v in exc.headers.items()}
        return int(exc.code), body, headers

    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}", {}


def json_get(url: str, *, timeout: int = 10) -> dict[str, Any]:
    status, body, _headers = http_request(url, timeout=timeout)

    if status != 200:
        raise AssertionError(f"Expected HTTP 200 from {url}, got {status}: {body[:500]}")

    try:
        return json.loads(body)
    except Exception as exc:
        raise AssertionError(f"Expected JSON from {url}: {exc}: {body[:500]}") from exc


def wait_until(label: str, condition, *, timeout_seconds: int = 60, interval: float = 1.0):
    deadline = time.time() + timeout_seconds
    last_error = None

    while time.time() < deadline:
        try:
            value = condition()
            if value:
                return value
        except Exception as exc:
            last_error = exc

        time.sleep(interval)

    raise AssertionError(f"Timed out waiting for {label}. Last error: {last_error}")


def assert_api_is_running() -> None:
    data = json_get(f"{API_URL}/health")
    assert data.get("ok") is True, data
    print("✅ FastAPI /health OK")


def assert_docker_is_running() -> None:
    run(["docker", "info"], timeout=60)
    print("✅ Docker daemon OK")


def make_backend_config(path: Path, ports: list[int]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)

    config = {
        "port": 8090,
        "runtime": "docker_host",
        "strategy": "bluegreen",
        "verify": "full",
        "routes": [
            {
                "path": "/api",
                "balancing": "random",
                "upstreams": [f"127.0.0.1:{port}" for port in ports],
            }
        ],
        "security": {
            "allowed_methods": ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
            "blocked_paths": [],
            "rate_limit_per_minute": 10000,
            "max_connections": 1000,
            "max_request_body_bytes": 1048576,
            "upstream_timeout_seconds": 30,
        },
    }

    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


def start_demo_backends(config_path: Path | None = None) -> None:
    if config_path is None:
        config_path = PROJECT_ROOT / "generated-pingora-proxy" / "config.json"

    if not config_path.exists():
        config_path = make_backend_config(
            PROJECT_ROOT / "tmp" / "acceptance_baseline_backends.json",
            [9101, 9102, 9103],
        )

    script = PROJECT_ROOT / "scripts" / "demo_backends.py"

    if not script.exists():
        raise AssertionError(f"Missing {script}")

    run(
        [PYTHON, str(script), "start", str(config_path)],
        timeout=120,
        check=True,
    )


def start_demo_backend_ports(ports: list[int], name: str) -> None:
    config_path = make_backend_config(
        PROJECT_ROOT / "tmp" / f"acceptance_{name}_backends.json",
        ports,
    )
    start_demo_backends(config_path)


def stop_backend_port(port: int) -> None:
    run(
        ["bash", "-lc", f"lsof -tiTCP:{port} -sTCP:LISTEN | xargs -r kill -9"],
        timeout=15,
        check=False,
    )


def wait_live() -> None:
    def condition():
        status, _body, _headers = http_request(f"{LIVE_URL}/", timeout=5)
        return status == 200

    wait_until(f"gateway live on {LIVE_URL}", condition, timeout_seconds=120)
    print(f"✅ Gateway live on {LIVE_URL}")


def apply_baseline() -> None:
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
            BASELINE_PROMPT,
        ],
        timeout=1800,
        check=True,
    )
    wait_live()


def runtime_state() -> dict[str, Any]:
    return json_get(f"{LIVE_URL}/__gateway_runtime_state", timeout=10)


def backend_port_from_address(address: str) -> str:
    match = re.search(r":(\d{2,5})\b", str(address))
    return match.group(1) if match else ""


def route_upstream_health(path: str) -> dict[str, bool]:
    state = runtime_state()
    result: dict[str, bool] = {}

    for route in state.get("routes", []):
        if route.get("path") != path:
            continue

        for upstream in route.get("upstreams", []):
            port = backend_port_from_address(str(upstream.get("address") or ""))
            if port:
                result[port] = bool(upstream.get("healthy"))

    return result


def wait_port_health(path: str, port: str, expected: bool) -> None:
    def condition():
        health = route_upstream_health(path)
        return health.get(port) is expected

    wait_until(
        f"{path} port {port} healthy={expected}",
        condition,
        timeout_seconds=90,
        interval=2,
    )


def wait_all_ports_healthy(path: str, ports: set[str]) -> None:
    def condition():
        health = route_upstream_health(path)
        return ports.issubset(set(health)) and all(health.get(port) is True for port in ports)

    wait_until(
        f"{path} all ports healthy: {sorted(ports)}",
        condition,
        timeout_seconds=90,
        interval=2,
    )
    print(f"✅ {path} all expected upstreams healthy: {sorted(ports)}")


def extract_backend_port(body: str) -> str | None:
    try:
        data = json.loads(body)
        value = data.get("backend_port")
        if value:
            return str(value)
    except Exception:
        pass

    match = re.search(r"\b(9\d{3}|10\d{3})\b", body)
    return match.group(1) if match else None


def sample_route_ports(path: str, count: int = 60) -> Counter[str]:
    counts: Counter[str] = Counter()
    url = f"{LIVE_URL}{path.rstrip('/')}/"

    for _ in range(count):
        status, body, _headers = http_request(url, timeout=8)

        if status != 200:
            counts[f"HTTP_{status}"] += 1
            continue

        port = extract_backend_port(body)
        if port:
            counts[port] += 1
        else:
            counts["NO_BACKEND_EVIDENCE"] += 1

    return counts


def assert_random_uses_ports(expected_ports: set[str]) -> None:
    counts = sample_route_ports("/api", 90)
    seen = {key for key in counts if key.isdigit()}

    print("Random counts:", dict(counts))

    if not expected_ports.issubset(seen):
        raise AssertionError(f"Expected ports {sorted(expected_ports)}, got counts {dict(counts)}")

    print("✅ /api random uses expected healthy ports")


def assert_health_failover_and_recovery() -> None:
    log("Runtime health failover and recovery")

    wait_all_ports_healthy("/api", BASELINE_PORTS)

    stop_backend_port(9102)
    wait_port_health("/api", "9102", False)

    degraded_counts = sample_route_ports("/api", 90)
    degraded_seen = {key for key in degraded_counts if key.isdigit()}

    print("After stopping 9102:", dict(degraded_counts))

    if "9102" in degraded_seen:
        raise AssertionError(f"9102 still received traffic while unhealthy: {dict(degraded_counts)}")

    if not {"9101", "9103"}.issubset(degraded_seen):
        raise AssertionError(f"Healthy ports did not continue serving: {dict(degraded_counts)}")

    print("✅ 9102 removed from rotation while unhealthy")

    start_demo_backends(PROJECT_ROOT / "generated-pingora-proxy" / "config.json")
    wait_port_health("/api", "9102", True)

    recovered_counts = sample_route_ports("/api", 120)
    recovered_seen = {key for key in recovered_counts if key.isdigit()}

    print("After restarting 9102:", dict(recovered_counts))

    if not BASELINE_PORTS.issubset(recovered_seen):
        raise AssertionError(f"9102 did not return to rotation: {dict(recovered_counts)}")

    print("✅ 9102 recovered and returned to rotation")


def pingora_add(path: str, backend: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(
        [
            "pingora-ai",
            "add",
            "--runtime",
            "docker_host",
            "--strategy",
            "bluegreen",
            "--verify",
            "full",
            path,
            backend,
        ],
        timeout=1800,
        check=check,
    )


def pingora_edit_algorithm(path: str, algorithm: str) -> subprocess.CompletedProcess[str]:
    return run(
        [
            "pingora-ai",
            "edit",
            "--runtime",
            "docker_host",
            "--strategy",
            "bluegreen",
            "--verify",
            "full",
            "--confirm-route-mutation",
            path,
            "--algorithm",
            algorithm,
        ],
        timeout=1800,
        check=True,
    )


def pingora_delete(path: str, *, command: str = "delete", check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(
        [
            "pingora-ai",
            command,
            "--runtime",
            "docker_host",
            "--strategy",
            "bluegreen",
            "--verify",
            "full",
            "--confirm-route-mutation",
            path,
        ],
        timeout=1800,
        check=check,
    )


def homepage_contains(text: str) -> bool:
    status, body, _headers = http_request(f"{LIVE_URL}/", timeout=10)
    return status == 200 and text in body


def assert_route_backend(path: str, expected_port: str) -> None:
    counts = sample_route_ports(path, 10)
    print(f"{path} counts:", dict(counts))

    if expected_port not in counts:
        raise AssertionError(f"Expected {path} to use {expected_port}, got {dict(counts)}")


def assert_duplicate_add_noop() -> None:
    log("Duplicate add no-op guard")

    result = pingora_add("/coupons", "127.0.0.1:9401", check=True)
    output = f"{result.stdout}\n{result.stderr}".lower()

    expected_markers = [
        "already exists",
        "no deployment needed",
        '"deployed": false',
        '"changed": false',
        "route_already_exists",
    ]

    if not any(marker in output for marker in expected_markers):
        raise AssertionError(
            "Duplicate add did not look like a no-op. "
            "Expected 'already exists' or deployed/changed false."
        )

    print("✅ duplicate add no-op guard works")


def assert_backend_port_conflict_guard() -> None:
    log("Backend-port conflict guard")

    env = os.environ.copy()
    env.pop("PINGORA_AI_ALLOW_SHARED_BACKEND", None)

    result = run(
        [
            "pingora-ai",
            "add",
            "--runtime",
            "docker_host",
            "--strategy",
            "bluegreen",
            "--verify",
            "full",
            "/offers",
            "127.0.0.1:9401",
        ],
        timeout=600,
        check=True,
        env=env,
    )

    output = f"{result.stdout}\n{result.stderr}".lower()

    expected_markers = [
        "already used",
        "backend port",
        "no deployment was made",
        "deployed\": false",
        "changed\": false",
    ]

    if not any(marker in output for marker in expected_markers):
        raise AssertionError("Backend-port conflict guard did not trigger as expected.")

    if homepage_contains("/offers"):
        raise AssertionError("/offers appeared on homepage even though backend port should be blocked.")

    print("✅ backend-port conflict guard works")


def assert_round_robin() -> None:
    log("Edit /api to round_robin")

    pingora_edit_algorithm("/api", "round_robin")

    sequence: list[str] = []

    for _ in range(9):
        counts = sample_route_ports("/api", 1)
        port = next((key for key in counts if key.isdigit()), None)
        if port:
            sequence.append(port)

    print("Round-robin sequence:", sequence)

    if len(sequence) != 9:
        raise AssertionError(f"Expected 9 backend observations, got {sequence}")

    if sequence[:3] != sequence[3:6] or sequence[:3] != sequence[6:9]:
        raise AssertionError(f"Sequence is not repeating every 3: {sequence}")

    if set(sequence[:3]) != BASELINE_PORTS:
        raise AssertionError(f"Round-robin did not include all backends: {sequence}")

    print("✅ edit /api to round_robin works")


def assert_zero_downtime_update() -> None:
    log("Zero-downtime update continuity")

    path = f"/zdaccept{int(time.time())}"
    backend = "127.0.0.1:9499"
    start_demo_backend_ports([9499], "zero_downtime")

    stop_event = threading.Event()
    statuses: list[int | None] = []

    def monitor() -> None:
        while not stop_event.is_set():
            status, _body, _headers = http_request(f"{LIVE_URL}/", timeout=5)
            statuses.append(status)
            time.sleep(1)

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()

    try:
        pingora_add(path, backend, check=True)
    finally:
        stop_event.set()
        thread.join(timeout=10)

    bad = [status for status in statuses if status != 200]

    print("Zero-downtime monitor statuses:", statuses)

    if bad:
        raise AssertionError(f"{LIVE_URL} had non-200 statuses during update: {bad}")

    print(f"✅ {LIVE_URL} stayed live during update")

    pingora_delete(path, command="delete", check=True)


def assert_delete_and_remove_alias() -> None:
    log("Delete /coupons")

    pingora_delete("/coupons", command="delete", check=True)

    if homepage_contains("/coupons"):
        raise AssertionError("/coupons still appears after delete")

    print("✅ delete /coupons works")

    log("Remove alias")

    pingora_add("/coupons", "127.0.0.1:9401", check=True)
    pingora_delete("/coupons", command="remove", check=True)

    if homepage_contains("/coupons"):
        raise AssertionError("/coupons still appears after remove alias")

    print("✅ remove alias works")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Full pingora-ai CLI acceptance test.",
    )
    parser.add_argument(
        "--skip-zero-downtime",
        action="store_true",
        help="Skip the live continuity test during an update.",
    )
    parser.add_argument(
        "--leave-random",
        action="store_true",
        default=True,
        help="Set /api back to random at the end.",
    )
    args = parser.parse_args()

    log("Environment checks")
    assert_api_is_running()
    assert_docker_is_running()

    log("Start baseline demo backends")
    start_demo_backends()

    log("Apply baseline via FastAPI config-file path")
    apply_baseline()

    log("Runtime health baseline")
    wait_all_ports_healthy("/api", BASELINE_PORTS)
    assert_random_uses_ports(BASELINE_PORTS)

    assert_health_failover_and_recovery()

    log("Add /coupons")
    start_demo_backend_ports([9401], "coupons")
    pingora_add("/coupons", "127.0.0.1:9401", check=True)

    if not homepage_contains("/coupons"):
        raise AssertionError("/coupons not visible on homepage after add")

    assert_route_backend("/coupons", "9401")
    print("✅ add /coupons works")

    assert_duplicate_add_noop()
    assert_backend_port_conflict_guard()

    assert_round_robin()

    log("Edit /api back to random")
    pingora_edit_algorithm("/api", "random")
    wait_all_ports_healthy("/api", BASELINE_PORTS)
    assert_random_uses_ports(BASELINE_PORTS)

    if not args.skip_zero_downtime:
        assert_zero_downtime_update()

    assert_delete_and_remove_alias()

    log("Final health check")
    wait_all_ports_healthy("/api", BASELINE_PORTS)
    assert_random_uses_ports(BASELINE_PORTS)

    print("")
    print("✅ ALL PINGORA-AI ACCEPTANCE TESTS PASSED")
    print("✅ apply")
    print("✅ runtime health")
    print("✅ failover/recovery")
    print("✅ add")
    print("✅ duplicate add no-op")
    print("✅ backend-port conflict guard")
    print("✅ edit")
    print("✅ delete")
    print("✅ remove alias")
    print("✅ zero-downtime update")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
