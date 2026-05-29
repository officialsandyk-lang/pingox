from __future__ import annotations

import argparse
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


def section(title: str) -> None:
    print("")
    print("=" * 90)
    print(title)
    print("=" * 90)


def run(
    command: list[str],
    *,
    timeout: int = 600,
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
            body = response.read().decode("utf-8", errors="replace")
            return int(response.status), body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return int(exc.code), body
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def json_get(url: str, timeout: int = 10) -> dict[str, Any]:
    status, body = http_request(url, timeout=timeout)

    if status != 200:
        raise AssertionError(f"Expected HTTP 200 from {url}, got {status}: {body[:300]}")

    return json.loads(body)


def wait_until(label: str, condition, *, timeout_seconds: int = 120, interval: float = 2.0):
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
    data = json_get(f"{API_URL}/health", timeout=10)
    assert data.get("ok") is True, data
    print("✅ FastAPI is running")


def ensure_docker() -> None:
    if (PROJECT_ROOT / "scripts" / "docker_ready.sh").exists():
        run(["bash", "scripts/docker_ready.sh", "180"], timeout=240, check=True)
    else:
        run(["open", "-a", "Docker"], timeout=20, check=False)
        run(["docker", "context", "use", "desktop-linux"], timeout=30, check=False)
        wait_until(
            "Docker daemon",
            lambda: run(["docker", "info"], timeout=30, check=False).returncode == 0,
            timeout_seconds=180,
            interval=3,
        )

    print("✅ Docker is connected")


def make_backend_config(path: Path, ports: list[int]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)

    config = {
        "port": 8090,
        "runtime": "docker_host",
        "strategy": "bluegreen",
        "routes": [
            {
                "path": "/api",
                "balancing": "random",
                "upstreams": [f"127.0.0.1:{port}" for port in ports],
            }
        ],
    }

    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


def start_demo_backends() -> None:
    config_path = PROJECT_ROOT / "generated-pingora-proxy" / "config.json"

    if not config_path.exists():
        config_path = make_backend_config(
            PROJECT_ROOT / "tmp" / "baseline_backends.json",
            [9101, 9102, 9103],
        )

    run(
        [PYTHON, "scripts/demo_backends.py", "start", str(config_path)],
        timeout=120,
        check=True,
    )


def stop_backend(port: int) -> None:
    run(
        ["bash", "-lc", f"lsof -tiTCP:{port} -sTCP:LISTEN | xargs -r kill -9"],
        timeout=20,
        check=False,
    )


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


def runtime_state() -> dict[str, Any]:
    return json_get(f"{LIVE_URL}/__gateway_runtime_state", timeout=10)


def port_from_address(address: str) -> str:
    match = re.search(r":(\d{2,5})\b", address)
    return match.group(1) if match else ""


def route_health(path: str = "/api") -> dict[str, bool]:
    state = runtime_state()
    health: dict[str, bool] = {}

    for route in state.get("routes", []):
        if route.get("path") != path:
            continue

        for upstream in route.get("upstreams", []):
            port = port_from_address(str(upstream.get("address") or ""))
            if port:
                health[port] = bool(upstream.get("healthy"))

    return health


def wait_all_healthy(path: str, ports: set[str]) -> None:
    wait_until(
        f"{path} all ports healthy",
        lambda: ports.issubset(set(route_health(path))) and all(route_health(path).get(p) is True for p in ports),
        timeout_seconds=120,
        interval=3,
    )
    print(f"✅ {path} all ports healthy: {sorted(ports)}")


def wait_port_health(path: str, port: str, expected: bool) -> None:
    wait_until(
        f"{path} port {port} healthy={expected}",
        lambda: route_health(path).get(port) is expected,
        timeout_seconds=120,
        interval=3,
    )
    print(f"✅ {path} port {port} healthy={expected}")


def assert_random_all_ports() -> None:
    counts = sample_route("/api", 120)
    seen = {key for key in counts if key.isdigit()}

    print("Random counts:", dict(counts))

    if not BASELINE_PORTS.issubset(seen):
        raise AssertionError(f"Expected {BASELINE_PORTS}, got {dict(counts)}")

    print("✅ /api random uses all expected ports")


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


def monitor_gateway(stop_event: threading.Event, statuses: list[int | None]) -> None:
    while not stop_event.is_set():
        status, _body = http_request(f"{LIVE_URL}/", timeout=5)
        statuses.append(status)
        time.sleep(1)


def run_with_zero_downtime_monitor(label: str, command: list[str], timeout: int = 1800) -> None:
    statuses: list[int | None] = []
    stop_event = threading.Event()
    thread = threading.Thread(target=monitor_gateway, args=(stop_event, statuses), daemon=True)

    print(f"🔎 Starting 8090 continuity monitor for: {label}")
    thread.start()

    try:
        run(command, timeout=timeout, check=True)
    finally:
        stop_event.set()
        thread.join(timeout=10)

    bad = [status for status in statuses if status != 200]

    print("Continuity statuses:", statuses)

    if bad:
        raise AssertionError(f"8090 had non-200 responses during {label}: {bad}")

    print(f"✅ Zero-downtime continuity passed for: {label}")


def pingora_add(path: str, backend: str, monitor: bool = True) -> subprocess.CompletedProcess[str] | None:
    command = [
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
    ]

    if monitor:
        run_with_zero_downtime_monitor(f"add {path}", command)
        return None

    return run(command, timeout=1800, check=True)


def pingora_edit(path: str, algorithm: str, monitor: bool = True) -> subprocess.CompletedProcess[str] | None:
    command = [
        "pingora-ai",
        "edit",
        "--runtime",
        "docker_host",
        "--strategy",
        "bluegreen",
        "--verify",
        "full",
        path,
        "--algorithm",
        algorithm,
    ]

    if monitor:
        run_with_zero_downtime_monitor(f"edit {path} {algorithm}", command)
        return None

    return run(command, timeout=1800, check=True)


def pingora_remove(path: str, command_name: str = "remove", monitor: bool = True) -> subprocess.CompletedProcess[str] | None:
    command = [
        "pingora-ai",
        command_name,
        "--runtime",
        "docker_host",
        "--strategy",
        "bluegreen",
        "--verify",
        "full",
        path,
    ]

    if monitor:
        run_with_zero_downtime_monitor(f"{command_name} {path}", command)
        return None

    return run(command, timeout=1800, check=True)


def homepage_contains(text: str) -> bool:
    status, body = http_request(f"{LIVE_URL}/", timeout=10)
    return status == 200 and text in body


def assert_route_backend(path: str, expected_port: str) -> None:
    counts = sample_route(path, 20)
    print(f"{path} counts:", dict(counts))

    if expected_port not in counts:
        raise AssertionError(f"Expected {path} to use backend {expected_port}, got {dict(counts)}")

    print(f"✅ {path} routes to {expected_port}")


def test_health_failover() -> None:
    section("Runtime health failover/recovery stress")

    wait_all_healthy("/api", BASELINE_PORTS)

    stop_backend(9102)
    wait_port_health("/api", "9102", False)

    counts_down = sample_route("/api", 120)
    print("Counts while 9102 is down:", dict(counts_down))

    if "9102" in counts_down:
        raise AssertionError(f"9102 received traffic while unhealthy: {dict(counts_down)}")

    if not {"9101", "9103"}.issubset(set(counts_down)):
        raise AssertionError(f"Healthy ports did not continue: {dict(counts_down)}")

    print("✅ 9102 skipped while unhealthy")

    start_demo_backends()
    wait_port_health("/api", "9102", True)

    counts_recovered = sample_route("/api", 120)
    print("Counts after 9102 recovery:", dict(counts_recovered))

    if not BASELINE_PORTS.issubset(set(counts_recovered)):
        raise AssertionError(f"9102 did not return after recovery: {dict(counts_recovered)}")

    print("✅ 9102 recovered and returned to rotation")


def test_add_edit_remove_stress(cycles: int) -> None:
    section(f"Add/edit/remove zero-downtime stress cycles={cycles}")

    for cycle in range(1, cycles + 1):
        print("")
        print(f"--- Cycle {cycle}/{cycles} ---")

        route = f"/stress{cycle}"
        backend_port = 9401 + cycle
        backend = f"127.0.0.1:{backend_port}"

        # Make sure backend exists.
        config_path = make_backend_config(
            PROJECT_ROOT / "tmp" / f"stress_backend_{backend_port}.json",
            [backend_port],
        )
        run([PYTHON, "scripts/demo_backends.py", "start", str(config_path)], timeout=120, check=True)

        pingora_add(route, backend, monitor=True)

        if not homepage_contains(route):
            raise AssertionError(f"{route} missing from homepage after add")

        assert_route_backend(route, str(backend_port))

        # Duplicate add should no-op.
        duplicate = pingora_add(route, backend, monitor=False)
        if duplicate:
            output = f"{duplicate.stdout}\n{duplicate.stderr}".lower()
            markers = ["already exists", "no deployment needed", '"deployed": false', '"changed": false']
            if not any(marker in output for marker in markers):
                raise AssertionError(f"Duplicate add did not no-op. Output: {output[-2000:]}")
        print("✅ duplicate add no-op works")

        # Edit /api to round_robin.
        pingora_edit("/api", "round_robin", monitor=True)

        sequence: list[str] = []
        for _ in range(9):
            counts = sample_route("/api", 1)
            port = next((key for key in counts if key.isdigit()), None)
            if port:
                sequence.append(port)

        print("Round-robin sequence:", sequence)

        if len(sequence) != 9:
            raise AssertionError(f"Bad round-robin observation: {sequence}")

        if sequence[:3] != sequence[3:6] or sequence[:3] != sequence[6:9]:
            raise AssertionError(f"Round-robin not repeating every 3: {sequence}")

        if set(sequence[:3]) != BASELINE_PORTS:
            raise AssertionError(f"Round-robin missing ports: {sequence}")

        print("✅ edit to round_robin works")

        # Edit back to random.
        pingora_edit("/api", "random", monitor=True)
        wait_all_healthy("/api", BASELINE_PORTS)
        assert_random_all_ports()

        # Remove stress route.
        pingora_remove(route, "remove", monitor=True)

        if homepage_contains(route):
            raise AssertionError(f"{route} still on homepage after remove")

        print(f"✅ {route} removed")


def docker_daemon_stop_recovery_test() -> None:
    section("Docker daemon stop/recovery hard-crash test")

    print(
        "⚠️ This test quits Docker Desktop. Containers will stop, so 8090 is expected to go down."
    )
    print("This is a recovery test, not a zero-downtime test.")

    wait_live()

    run(["osascript", "-e", 'quit app "Docker"'], timeout=30, check=False)

    time.sleep(10)

    docker_down = run(["docker", "info"], timeout=20, check=False).returncode != 0
    print("Docker daemon down:", docker_down)

    if not docker_down:
        print("⚠️ Docker still appears connected. Continuing recovery check anyway.")

    status, _body = http_request(f"{LIVE_URL}/", timeout=5)
    print("8090 status after Docker quit:", status)

    # Restart Docker.
    ensure_docker()

    # Start demo backends, then recover with a clean apply.
    start_demo_backends()
    apply_baseline()
    wait_all_healthy("/api", BASELINE_PORTS)
    assert_random_all_ports()

    print("✅ Docker daemon stop/recovery test passed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles", type=int, default=1, help="Add/edit/remove stress cycles.")
    parser.add_argument(
        "--test-docker-daemon-stop",
        action="store_true",
        help="Quit Docker Desktop and verify recovery. 8090 will go down during this test.",
    )
    args = parser.parse_args()

    section("Preflight")
    ensure_fastapi()
    ensure_docker()

    section("Start demo backends")
    start_demo_backends()

    section("Apply baseline")
    apply_baseline()

    section("Runtime health baseline")
    wait_all_healthy("/api", BASELINE_PORTS)
    assert_random_all_ports()

    test_health_failover()
    test_add_edit_remove_stress(args.cycles)

    section("Final baseline health")
    wait_all_healthy("/api", BASELINE_PORTS)
    assert_random_all_ports()

    if args.test_docker_daemon_stop:
        docker_daemon_stop_recovery_test()

    print("")
    print("✅ FINAL STRESS TEST PASSED")
    print("✅ apply")
    print("✅ runtime health failover/recovery")
    print("✅ add")
    print("✅ duplicate add no-op")
    print("✅ edit round_robin/random")
    print("✅ remove")
    print("✅ zero-downtime during Docker blue/green updates")
    if args.test_docker_daemon_stop:
        print("✅ Docker daemon stop/recovery")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
