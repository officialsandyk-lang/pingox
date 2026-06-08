from __future__ import annotations

import json
import os
import re
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
LIVE_URL = "http://127.0.0.1:8090"

BASELINE_PROMPT = (
    "create gateway with /api random across "
    "127.0.0.1:9101, 127.0.0.1:9102 and 127.0.0.1:9103"
)

BASELINE_PORTS = {"9101", "9102", "9103"}
ALL_TEST_PORTS = [9101, 9102, 9103, 9408, 9409]


def section(title: str) -> None:
    print("")
    print("=" * 100)
    print(title)
    print("=" * 100)


def run(
    command: list[str],
    *,
    timeout: int = 900,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    print("")
    print("$ " + " ".join(command))

    merged_env = os.environ.copy()
    merged_env.pop("DOCKER_HOST", None)
    merged_env["DOCKER_CONTEXT"] = "desktop-linux"

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
        print(result.stdout[-10000:])

    if result.stderr.strip():
        print(result.stderr[-6000:])

    if check and result.returncode != 0:
        raise AssertionError(
            f"Command failed with exit code {result.returncode}: {' '.join(command)}"
        )

    return result


def http_request(url: str, timeout: int = 10) -> tuple[int | None, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return int(response.status), response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def json_get(url: str, timeout: int = 10) -> dict[str, Any]:
    status, body = http_request(url, timeout=timeout)

    if status != 200:
        raise AssertionError(f"Expected 200 from {url}, got {status}: {body[:300]}")

    return json.loads(body)


def wait_until(label: str, condition, timeout_seconds: int = 180, interval: float = 2.0):
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


def ensure_fastapi() -> None:
    data = json_get(f"{API_URL}/health")
    if data.get("ok") is not True:
        raise AssertionError(f"FastAPI health failed: {data}")
    print("✅ FastAPI is running")


def ensure_docker() -> None:
    if (PROJECT_ROOT / "scripts" / "docker_ready.sh").exists():
        run(["bash", "scripts/docker_ready.sh", "180"], timeout=240)
    else:
        run(["open", "-a", "Docker"], timeout=30, check=False)
        wait_until(
            "Docker daemon",
            lambda: run(["docker", "info"], timeout=30, check=False).returncode == 0,
            timeout_seconds=180,
            interval=3,
        )
    print("✅ Docker is connected")


def kill_port(port: int) -> None:
    run(
        ["bash", "-lc", f"lsof -tiTCP:{port} -sTCP:LISTEN | xargs -r kill -9"],
        timeout=30,
        check=False,
    )


def cleanup_runtime_keep_fastapi() -> None:
    section("Cleanup runtime while keeping FastAPI alive")

    kill_port(8090)

    for port in ALL_TEST_PORTS:
        kill_port(port)

    # Clean Docker if daemon is available.
    if run(["docker", "--context", "desktop-linux", "info"], timeout=30, check=False).returncode == 0:
        run(
            [
                "docker",
                "--context",
                "desktop-linux",
                "compose",
                "-f",
                "docker-bluegreen/docker-compose.yml",
                "down",
                "--remove-orphans",
            ],
            timeout=180,
            check=False,
        )
        run(
            [
                "docker",
                "--context",
                "desktop-linux",
                "rm",
                "-f",
                "ai-pingora-gateway-blue",
                "ai-pingora-gateway-green",
                "ai-pingora-gateway-edge",
            ],
            timeout=60,
            check=False,
        )
        run(
            [
                "docker",
                "--context",
                "desktop-linux",
                "network",
                "rm",
                "docker-bluegreen_ai_pingora_net",
            ],
            timeout=60,
            check=False,
        )

    print("✅ Runtime cleanup completed")


def write_backend_config(name: str, ports: list[int]) -> Path:
    path = PROJECT_ROOT / "tmp" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)

    config = {
        "port": 8090,
        "runtime": "local",
        "strategy": "direct",
        "routes": [
            {
                "path": f"/backend-{ports[0]}",
                "balancing": "single",
                "upstreams": [f"127.0.0.1:{port}" for port in ports],
            }
        ],
    }

    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


def start_demo_backends(ports: list[int]) -> None:
    path = write_backend_config("stress_runtime_switch_backends", ports)
    run([PYTHON, "scripts/demo_backends.py", "start", str(path)], timeout=120)


def wait_live() -> None:
    wait_until(
        "gateway live on 8090",
        lambda: http_request(f"{LIVE_URL}/", timeout=5)[0] == 200,
        timeout_seconds=180,
    )
    print("✅ Gateway live on 8090")


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


def sample_route(path: str, count: int = 90) -> Counter[str]:
    counts: Counter[str] = Counter()
    url = f"{LIVE_URL}{path.rstrip('/')}/"

    for _ in range(count):
        status, body = http_request(url, timeout=10)

        if status != 200:
            counts[f"HTTP_{status}"] += 1
            continue

        port = extract_backend_port(body)
        if port:
            counts[port] += 1
        else:
            counts["NO_BACKEND"] += 1

    return counts


def assert_random_api() -> None:
    counts = sample_route("/api", 120)
    seen = {key for key in counts if key.isdigit()}

    print("Random counts:", dict(counts))

    if not BASELINE_PORTS.issubset(seen):
        raise AssertionError(f"/api random missing expected ports: {dict(counts)}")

    print("✅ /api random uses all expected backends")


def runtime_state() -> dict[str, Any]:
    return json_get(f"{LIVE_URL}/__gateway_runtime_state")


def runtime_health(path: str = "/api") -> dict[str, bool]:
    state = runtime_state()
    result: dict[str, bool] = {}

    for route in state.get("routes", []):
        if route.get("path") != path:
            continue

        for upstream in route.get("upstreams", []):
            address = str(upstream.get("address") or "")
            match = re.search(r":(\d{2,5})\b", address)
            if match:
                result[match.group(1)] = bool(upstream.get("healthy"))

    return result


def wait_api_all_healthy() -> None:
    wait_until(
        "/api all upstreams healthy",
        lambda: BASELINE_PORTS.issubset(set(runtime_health())) and all(
            runtime_health().get(port) is True for port in BASELINE_PORTS
        ),
        timeout_seconds=180,
        interval=3,
    )
    print("✅ /api runtime health all green")


def wait_port_health(port: str, expected: bool) -> None:
    wait_until(
        f"/api port {port} healthy={expected}",
        lambda: runtime_health().get(port) is expected,
        timeout_seconds=180,
        interval=3,
    )
    print(f"✅ /api port {port} healthy={expected}")


def apply_gateway(runtime: str, strategy: str) -> None:
    run(
        [
            "pingora-ai",
            "apply",
            "--runtime",
            runtime,
            "--strategy",
            strategy,
            "--verify",
            "full",
            BASELINE_PROMPT,
        ],
        timeout=1800,
    )
    wait_live()
    wait_api_all_healthy()
    assert_random_api()


def verify_addressing(expected: str, forbidden: str) -> None:
    files = [
        PROJECT_ROOT / "generated-pingora-proxy" / "config.json",
        PROJECT_ROOT / "generated-pingora-proxy" / "src" / "main.rs",
        PROJECT_ROOT / "generated-pingora-proxy" / "public" / "index.html",
    ]

    content = "\n".join(path.read_text(encoding="utf-8") for path in files if path.exists())

    if expected not in content:
        raise AssertionError(f"Expected addressing not found: {expected}")

    if forbidden in content:
        raise AssertionError(f"Forbidden addressing found: {forbidden}")

    print(f"✅ Addressing OK: found {expected}, no {forbidden}")


def monitor_8090(stop_event: threading.Event, statuses: list[int | None]) -> None:
    while not stop_event.is_set():
        status, _body = http_request(f"{LIVE_URL}/", timeout=5)
        statuses.append(status)
        time.sleep(1)


def run_update_with_optional_monitor(label: str, command: list[str], monitor: bool) -> subprocess.CompletedProcess[str] | None:
    if not monitor:
        return run(command, timeout=1800)

    statuses: list[int | None] = []
    stop_event = threading.Event()
    thread = threading.Thread(target=monitor_8090, args=(stop_event, statuses), daemon=True)

    print(f"🔎 Monitoring 8090 during: {label}")
    thread.start()

    try:
        run(command, timeout=1800)
    finally:
        stop_event.set()
        thread.join(timeout=10)

    bad = [status for status in statuses if status != 200]
    print("Continuity statuses:", statuses)

    if bad:
        raise AssertionError(f"8090 had non-200 responses during {label}: {bad}")

    print(f"✅ Zero-downtime continuity passed for: {label}")
    return None



def add_route(runtime: str, strategy: str, route: str, backend: str, monitor: bool):
    command = [
        "pingora-ai",
        "add",
        "--runtime",
        runtime,
        "--strategy",
        strategy,
        "--verify",
        "full",
        route,
        backend,
    ]

    if monitor:
        return run_update_with_optional_monitor(
            f"add {route}",
            command,
            monitor,
        )

    print("")
    print("$ " + " ".join(command))

    result = subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        text=True,
        capture_output=True,
        timeout=1800,
    )

    if result.stdout.strip():
        print(result.stdout[-12000:])

    if result.stderr.strip():
        print(result.stderr[-6000:])

    output = f"{result.stdout}\n{result.stderr}".lower()

    noop_markers = [
        "already exists",
        "no deployment needed",
        '"deployed": false',
        '"changed": false',
        "no effective config changes detected",
        "no changes were deployed",
        "route update warning",
        "you are about to replace existing route",
        "system_decision_skipped",
        "no_effective_config_change",
    ]

    if result.returncode != 0 and not any(marker in output for marker in noop_markers):
        raise AssertionError(
            f"Command failed with exit code {result.returncode}: {' '.join(command)}"
        )

    return result


def edit_api(runtime: str, strategy: str, algorithm: str, monitor: bool) -> None:
    # Use confirmation phrase because the local update path reliably recognises it.
    run_update_with_optional_monitor(
        f"edit /api {algorithm}",
        [
            "pingora-ai",
            "update",
            "--runtime",
            runtime,
            "--strategy",
            strategy,
            "--verify",
            "full",
            f"set /api algorithm to {algorithm} CONFIRM ROUTE MUTATION",
        ],
        monitor,
    )


def remove_route(runtime: str, strategy: str, route: str, monitor: bool) -> None:
    # Use confirmation phrase because the local update path reliably recognises it.
    run_update_with_optional_monitor(
        f"remove {route}",
        [
            "pingora-ai",
            "update",
            "--runtime",
            runtime,
            "--strategy",
            strategy,
            "--verify",
            "full",
            f"remove {route} CONFIRM ROUTE MUTATION",
        ],
        monitor,
    )

def assert_route_backend(path: str, port: str) -> None:
    counts = sample_route(path, 20)
    print(f"{path} counts:", dict(counts))

    if port not in counts:
        raise AssertionError(f"{path} did not route to {port}: {dict(counts)}")

    print(f"✅ {path} routes to {port}")


def homepage_contains(text: str) -> bool:
    status, body = http_request(f"{LIVE_URL}/")
    return status == 200 and text in body


def assert_round_robin_cycle() -> None:
    sequence: list[str] = []

    for _ in range(9):
        counts = sample_route("/api", 1)
        port = next((key for key in counts if key.isdigit()), None)
        if port:
            sequence.append(port)

    print("Round-robin sequence:", sequence)

    if len(sequence) != 9:
        raise AssertionError(f"Expected 9 observations, got {sequence}")

    if sequence[:3] != sequence[3:6] or sequence[:3] != sequence[6:9]:
        raise AssertionError(f"Round-robin did not repeat cleanly: {sequence}")

    if set(sequence[:3]) != BASELINE_PORTS:
        raise AssertionError(f"Round-robin missing ports: {sequence}")

    print("✅ /api round_robin cycle verified")


def test_add_edit_remove(runtime: str, strategy: str, route: str, backend_port: int, monitor: bool) -> None:
    section(f"{runtime}/{strategy}: add/edit/remove")

    start_demo_backends([backend_port])

    add_route(runtime, strategy, route, f"127.0.0.1:{backend_port}", monitor)

    if not homepage_contains(route):
        raise AssertionError(f"{route} missing from homepage after add")

    assert_route_backend(route, str(backend_port))

    try:
        duplicate = add_route(runtime, strategy, route, f"127.0.0.1:{backend_port}", False)
        output = f"{duplicate.stdout}\n{duplicate.stderr}".lower() if duplicate else ""
    except AssertionError as exc:
        output = str(exc).lower()

    markers = [
        "already exists",
        "no deployment needed",
        '"deployed": false',
        '"changed": false',
        "no effective config changes detected",
        "no changes were deployed",
        "route update warning",
        "you are about to replace existing route",
        "system_decision_skipped",
        "no_effective_config_change",
    ]

    if not any(marker in output for marker in markers):
        raise AssertionError(f"Duplicate add did not no-op: {output[-2000:]}")

    print("✅ Duplicate add no-op verified")

    
    edit_api(runtime, strategy, "round_robin", monitor)
    assert_round_robin_cycle()

    edit_api(runtime, strategy, "random", monitor)
    wait_api_all_healthy()
    assert_random_api()

    remove_route(runtime, strategy, route, monitor)

    if homepage_contains(route):
        raise AssertionError(f"{route} still appears after remove")

    print(f"✅ {route} removed")


def test_health_failover() -> None:
    section("Runtime health failover")

    wait_api_all_healthy()

    kill_port(9102)
    wait_port_health("9102", False)

    counts = sample_route("/api", 120)
    print("Counts while 9102 down:", dict(counts))

    if "9102" in counts:
        raise AssertionError(f"9102 received traffic while unhealthy: {dict(counts)}")

    if not {"9101", "9103"}.issubset({key for key in counts if key.isdigit()}):
        raise AssertionError(f"Healthy backends did not continue: {dict(counts)}")

    print("✅ unhealthy backend skipped")

    start_demo_backends([9102])
    wait_port_health("9102", True)
    assert_random_api()

    print("✅ backend recovery verified")


def main() -> int:
    section("Preflight")
    ensure_fastapi()

    section("Start baseline demo backends")
    start_demo_backends([9101, 9102, 9103])

    section("Local/direct apply")
    cleanup_runtime_keep_fastapi()
    start_demo_backends([9101, 9102, 9103])
    apply_gateway("local", "direct")
    verify_addressing("127.0.0.1:9101", "host.docker.internal")
    test_health_failover()
    test_add_edit_remove("local", "direct", "/localswitch", 9408, monitor=False)

    section("Switch local → Docker blue/green")
    cleanup_runtime_keep_fastapi()
    ensure_docker()
    start_demo_backends([9101, 9102, 9103])
    apply_gateway("docker_host", "bluegreen")
    verify_addressing("host.docker.internal:9101", "127.0.0.1:9101")
    test_health_failover()
    test_add_edit_remove("docker_host", "bluegreen", "/dockerswitch", 9409, monitor=True)

    section("Switch Docker → local/direct")
    cleanup_runtime_keep_fastapi()
    start_demo_backends([9101, 9102, 9103])
    apply_gateway("local", "direct")
    verify_addressing("127.0.0.1:9101", "host.docker.internal")
    wait_api_all_healthy()
    assert_random_api()

    print("")
    print("✅ FINAL RUNTIME SWITCH PRODUCTION STRESS TEST PASSED")
    print("✅ local/direct config-file deploy")
    print("✅ docker_host/bluegreen config-file deploy")
    print("✅ local ⇄ docker switching")
    print("✅ runtime health failover/recovery")
    print("✅ add/edit/remove")
    print("✅ duplicate add no-op")
    print("✅ Docker zero-downtime update continuity")
    print("✅ runtime-specific addressing")
    print("✅ full verification gates")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
