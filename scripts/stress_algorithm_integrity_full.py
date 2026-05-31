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

BASELINE_PROMPT = """
create gateway on port 8090 with:
- /api random across 127.0.0.1:9101, 127.0.0.1:9102, 127.0.0.1:9103
- /users round_robin across 127.0.0.1:9101, 127.0.0.1:9102, 127.0.0.1:9103
- /orders weighted_round_robin across 127.0.0.1:9201 weight 5, 127.0.0.1:9202 weight 2, 127.0.0.1:9203 weight 1
- /payments ip_hash across 127.0.0.1:9301 and 127.0.0.1:9302
- /reports least_connections across 127.0.0.1:9501, 127.0.0.1:9502, 127.0.0.1:9503
- /inventory to backend 127.0.0.1:9401
Set rate limit to 10000 requests per minute.
""".strip()

BASELINE_EXPECTED_ALGORITHMS = {
    "/api": "random",
    "/users": "round_robin",
    "/orders": "weighted_round_robin",
    "/payments": "ip_hash",
    "/reports": "least_connections",
    "/inventory": "single",
}

BASELINE_PORTS = {
    "/api": {"9101", "9102", "9103"},
    "/users": {"9101", "9102", "9103"},
    "/orders": {"9201", "9202", "9203"},
    "/payments": {"9301", "9302"},
    "/reports": {"9501", "9502", "9503"},
    "/inventory": {"9401"},
}


def deterministic_algorithm_config(runtime: str, strategy: str) -> dict[str, Any]:
    return {
        "port": 8090,
        "runtime": runtime,
        "runtime_mode": runtime,
        "strategy": strategy,
        "deployment_strategy": strategy,
        "security": {
            "rate_limit_per_minute": 10000,
        },
        "routes": [
            {
                "path": "/",
                "type": "static",
                "route_type": "static",
                "root": "public",
                "index": "index.html",
                "balancing": "single",
                "upstreams": [],
            },
            {
                "path": "/api",
                "type": "proxy",
                "route_type": "proxy",
                "balancing": "random",
                "upstreams": [
                    "127.0.0.1:9101",
                    "127.0.0.1:9102",
                    "127.0.0.1:9103",
                ],
            },
            {
                "path": "/users",
                "type": "proxy",
                "route_type": "proxy",
                "balancing": "round_robin",
                "upstreams": [
                    "127.0.0.1:9101",
                    "127.0.0.1:9102",
                    "127.0.0.1:9103",
                ],
            },
            {
                "path": "/orders",
                "type": "proxy",
                "route_type": "proxy",
                "balancing": "weighted_round_robin",
                "upstreams": [
                    {"address": "127.0.0.1:9201", "weight": 5},
                    {"address": "127.0.0.1:9202", "weight": 2},
                    {"address": "127.0.0.1:9203", "weight": 1},
                ],
            },
            {
                "path": "/payments",
                "type": "proxy",
                "route_type": "proxy",
                "balancing": "ip_hash",
                "upstreams": [
                    "127.0.0.1:9301",
                    "127.0.0.1:9302",
                ],
            },
            {
                "path": "/reports",
                "type": "proxy",
                "route_type": "proxy",
                "balancing": "least_connections",
                "upstreams": [
                    "127.0.0.1:9501",
                    "127.0.0.1:9502",
                    "127.0.0.1:9503",
                ],
            },
            {
                "path": "/inventory",
                "type": "proxy",
                "route_type": "proxy",
                "balancing": "single",
                "upstreams": [
                    "127.0.0.1:9401",
                ],
            },
        ],
    }


def write_deterministic_config(runtime: str, strategy: str) -> Path:
    config_path = PROJECT_ROOT / "tmp" / f"algorithm_integrity_{runtime}_{strategy}.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(deterministic_algorithm_config(runtime, strategy), indent=2),
        encoding="utf-8",
    )
    return config_path

ALL_BACKEND_PORTS = [
    9101, 9102, 9103,
    9201, 9202, 9203,
    9301, 9302,
    9401,
    9499,
    9501, 9502, 9503,
]


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
) -> subprocess.CompletedProcess[str]:
    print("")
    print("$ " + " ".join(command))

    env = os.environ.copy()
    env.pop("DOCKER_HOST", None)
    env["DOCKER_CONTEXT"] = "desktop-linux"

    result = subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env,
    )

    if result.stdout.strip():
        print(result.stdout[-12000:])

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
        raise AssertionError(f"Expected 200 from {url}, got {status}: {body[:400]}")
    return json.loads(body)


def wait_until(label: str, condition, *, timeout_seconds: int = 180, interval: float = 2.0):
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
        raise AssertionError(f"FastAPI unhealthy: {data}")
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
    print("✅ Docker connected")


def kill_port(port: int) -> None:
    run(
        ["bash", "-lc", f"lsof -tiTCP:{port} -sTCP:LISTEN | xargs -r kill -9"],
        timeout=30,
        check=False,
    )


def cleanup_runtime_keep_fastapi() -> None:
    section("Cleanup runtime while keeping FastAPI alive")

    kill_port(8090)
    for port in ALL_BACKEND_PORTS:
        kill_port(port)

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


def write_backend_config() -> Path:
    path = PROJECT_ROOT / "tmp" / "algorithm_integrity_backends.json"
    path.parent.mkdir(parents=True, exist_ok=True)

    config = {
        "port": 8090,
        "runtime": "local",
        "strategy": "direct",
        "routes": [
            {
                "path": "/api",
                "balancing": "random",
                "upstreams": [f"127.0.0.1:{port}" for port in ALL_BACKEND_PORTS],
            }
        ],
    }

    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


def start_demo_backends(ports: list[int] | None = None) -> None:
    if ports is None:
        ports = ALL_BACKEND_PORTS

    path = PROJECT_ROOT / "tmp" / "algorithm_integrity_backends_selected.json"
    path.parent.mkdir(parents=True, exist_ok=True)

    config = {
        "port": 8090,
        "runtime": "local",
        "strategy": "direct",
        "routes": [
            {
                "path": "/api",
                "balancing": "random",
                "upstreams": [f"127.0.0.1:{port}" for port in ports],
            }
        ],
    }

    path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    run([PYTHON, "scripts/demo_backends.py", "start", str(path)], timeout=120)


def wait_live() -> None:
    wait_until(
        "gateway live on 8090",
        lambda: http_request(f"{LIVE_URL}/", timeout=5)[0] == 200,
        timeout_seconds=180,
        interval=2,
    )
    print("✅ Gateway live on 8090")


def extract_backend_port(body: str) -> str | None:
    try:
        data = json.loads(body)
        value = data.get("backend_port")
        if value is not None:
            return str(value)
    except Exception:
        pass

    match = re.search(r"\b(9\d{3}|10\d{3})\b", body)
    return match.group(1) if match else None


def sample_route(path: str, count: int = 60) -> Counter[str]:
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


def observe_sequence(path: str, count: int) -> list[str]:
    sequence: list[str] = []
    url = f"{LIVE_URL}{path.rstrip('/')}/"

    for _ in range(count):
        status, body = http_request(url, timeout=10)
        if status != 200:
            sequence.append(f"HTTP_{status}")
            continue
        port = extract_backend_port(body)
        sequence.append(port or "NO_BACKEND")

    return sequence


def load_config() -> dict[str, Any]:
    path = PROJECT_ROOT / "generated-pingora-proxy" / "config.json"
    return json.loads(path.read_text(encoding="utf-8"))


def route_map() -> dict[str, dict[str, Any]]:
    config = load_config()
    return {
        str(route.get("path")): route
        for route in config.get("routes", [])
        if isinstance(route, dict)
    }


def route_algorithm(route: dict[str, Any]) -> str:
    balancing = route.get("balancing")
    if balancing:
        return str(balancing)

    upstreams = route.get("upstreams") or []
    if len(upstreams) <= 1:
        return "single"

    return "round_robin"


def upstream_ports(route: dict[str, Any]) -> set[str]:
    result: set[str] = set()

    for item in route.get("upstreams", []) or []:
        if isinstance(item, dict):
            text = str(item.get("address") or item.get("url") or item)
        else:
            text = str(item)

        match = re.search(r":(\d{2,5})\b", text)
        if match:
            result.add(match.group(1))

    return result


def assert_config_algorithms(expected: dict[str, str] | None = None) -> None:
    expected = expected or BASELINE_EXPECTED_ALGORITHMS
    routes = route_map()

    print("Config algorithms:")
    for path, algorithm in expected.items():
        if path not in routes:
            raise AssertionError(f"Missing expected route in config: {path}")

        actual = route_algorithm(routes[path])

        if actual != algorithm:
            raise AssertionError(f"{path} algorithm mismatch: expected {algorithm}, got {actual}")

        print(f"✅ {path}: {actual}")

    print("✅ Config algorithm integrity passed")


def assert_config_upstreams() -> None:
    routes = route_map()

    for path, expected_ports in BASELINE_PORTS.items():
        if path not in routes:
            raise AssertionError(f"Missing expected route: {path}")

        actual_ports = upstream_ports(routes[path])

        if not expected_ports.issubset(actual_ports):
            raise AssertionError(f"{path} upstream ports mismatch. Expected {expected_ports}, got {actual_ports}")

        print(f"✅ {path} upstream ports: {sorted(actual_ports)}")

    print("✅ Config upstream integrity passed")


def verify_addressing(runtime: str) -> None:
    files = [
        PROJECT_ROOT / "generated-pingora-proxy" / "config.json",
        PROJECT_ROOT / "generated-pingora-proxy" / "src" / "main.rs",
        PROJECT_ROOT / "generated-pingora-proxy" / "public" / "index.html",
    ]

    content = "\n".join(path.read_text(encoding="utf-8") for path in files if path.exists())

    if runtime == "local":
        expected = "127.0.0.1:9101"
        forbidden = "host.docker.internal"
    else:
        expected = "host.docker.internal:9101"
        forbidden = '"127.0.0.1:9101"'

    if expected not in content:
        raise AssertionError(f"Expected addressing missing for {runtime}: {expected}")

    if forbidden in content:
        raise AssertionError(f"Forbidden addressing leaked for {runtime}: {forbidden}")

    print(f"✅ Addressing integrity passed for {runtime}")


def runtime_state() -> dict[str, Any]:
    return json_get(f"{LIVE_URL}/__gateway_runtime_state")


def runtime_health(path: str = "/api") -> dict[str, bool]:
    data = runtime_state()
    result: dict[str, bool] = {}

    for route in data.get("routes", []):
        if route.get("path") != path:
            continue

        for upstream in route.get("upstreams", []):
            address = str(upstream.get("address") or "")
            match = re.search(r":(\d{2,5})\b", address)
            if match:
                result[match.group(1)] = bool(upstream.get("healthy"))

    return result


def wait_route_all_healthy(path: str, expected_ports: set[str]) -> None:
    wait_until(
        f"{path} all upstreams healthy",
        lambda: expected_ports.issubset(set(runtime_health(path))) and all(
            runtime_health(path).get(port) is True for port in expected_ports
        ),
        timeout_seconds=180,
        interval=3,
    )
    print(f"✅ Runtime health all green for {path}: {sorted(expected_ports)}")


def wait_port_health(path: str, port: str, expected: bool) -> None:
    wait_until(
        f"{path} {port} healthy={expected}",
        lambda: runtime_health(path).get(port) is expected,
        timeout_seconds=180,
        interval=3,
    )
    print(f"✅ Runtime health {path} {port} healthy={expected}")


def apply_gateway(runtime: str, strategy: str) -> None:
    section(f"Apply deterministic algorithm baseline: {runtime}/{strategy}")

    config_path = write_deterministic_config(runtime, strategy)

    run(
        [
            PYTHON,
            "main.py",
            "--runtime",
            runtime,
            "--strategy",
            strategy,
            "--verify",
            "full",
            "--config-file",
            str(config_path),
            "__algorithm_integrity_config_file_deploy__",
        ],
        timeout=1800,
    )

    wait_live()
    assert_config_algorithms()
    assert_config_upstreams()
    verify_addressing(runtime)
    wait_route_all_healthy("/api", {"9101", "9102", "9103"})


def verify_random(path: str, expected_ports: set[str], count: int = 150) -> None:
    counts = sample_route(path, count)
    seen = {key for key in counts if key.isdigit()}

    print(f"{path} random counts:", dict(counts))

    if not expected_ports.issubset(seen):
        raise AssertionError(f"{path} random missing expected ports. Expected {expected_ports}, got {dict(counts)}")

    print(f"✅ {path} random algorithm uses all backends")


def verify_round_robin(path: str, expected_ports: set[str], count: int = 12) -> None:
    sequence = observe_sequence(path, count)
    print(f"{path} round_robin sequence:", sequence)

    if any(not item.isdigit() for item in sequence):
        raise AssertionError(f"{path} round_robin had bad responses: {sequence}")

    if set(sequence[: len(expected_ports)]) != expected_ports:
        raise AssertionError(f"{path} round_robin missing ports in first cycle: {sequence}")

    cycle = sequence[: len(expected_ports)]
    for index, value in enumerate(sequence):
        if value != cycle[index % len(cycle)]:
            raise AssertionError(f"{path} round_robin did not repeat. Sequence={sequence}")

    print(f"✅ {path} round_robin cycle verified")


def verify_weighted_round_robin(path: str, expected_counts: dict[str, int], count: int = 80, tolerance: int = 2) -> None:
    counts = sample_route(path, count)
    print(f"{path} weighted counts:", dict(counts))

    for port, expected in expected_counts.items():
        actual = counts.get(port, 0)
        if abs(actual - expected) > tolerance:
            raise AssertionError(
                f"{path} weighted count mismatch for {port}: expected {expected}±{tolerance}, got {actual}; counts={dict(counts)}"
            )

    print(f"✅ {path} weighted_round_robin ratio verified")


def verify_ip_hash(path: str, expected_ports: set[str], count: int = 40) -> None:
    counts = sample_route(path, count)
    seen = {key for key in counts if key.isdigit()}

    print(f"{path} ip_hash counts:", dict(counts))

    if len(seen) != 1:
        raise AssertionError(f"{path} ip_hash was not sticky from same client: {dict(counts)}")

    if not seen.issubset(expected_ports):
        raise AssertionError(f"{path} ip_hash routed to unexpected backend: {dict(counts)}")

    print(f"✅ {path} ip_hash sticky verified")


def verify_least_connections(path: str, expected_ports: set[str], count: int = 60, tolerance: int = 8) -> None:
    counts = sample_route(path, count)
    seen = {key for key in counts if key.isdigit()}

    print(f"{path} least_connections counts:", dict(counts))

    if not expected_ports.issubset(seen):
        raise AssertionError(f"{path} least_connections missing ports. Expected {expected_ports}, got {dict(counts)}")

    values = [counts.get(port, 0) for port in expected_ports]
    if max(values) - min(values) > tolerance:
        raise AssertionError(f"{path} least_connections too imbalanced: {dict(counts)}")

    print(f"✅ {path} least_connections distribution verified")


def verify_single(path: str, expected_port: str, count: int = 20) -> None:
    counts = sample_route(path, count)
    print(f"{path} single counts:", dict(counts))

    if set(counts.keys()) != {expected_port}:
        raise AssertionError(f"{path} single backend mismatch. Expected only {expected_port}, got {dict(counts)}")

    print(f"✅ {path} single backend verified")


def verify_all_live_algorithms() -> None:
    section("Live algorithm integrity")

    verify_random("/api", {"9101", "9102", "9103"})
    verify_round_robin("/users", {"9101", "9102", "9103"}, count=12)
    verify_weighted_round_robin(
        "/orders",
        {"9201": 50, "9202": 20, "9203": 10},
        count=80,
        tolerance=2,
    )
    verify_ip_hash("/payments", {"9301", "9302"})
    verify_least_connections("/reports", {"9501", "9502", "9503"})
    verify_single("/inventory", "9401")


def verify_health_failover() -> None:
    section("Runtime health failover integrity")

    wait_route_all_healthy("/api", {"9101", "9102", "9103"})

    kill_port(9102)
    wait_port_health("/api", "9102", False)

    counts = sample_route("/api", 120)
    print("Counts while 9102 down:", dict(counts))

    if "9102" in counts:
        raise AssertionError(f"Unhealthy 9102 still received traffic: {dict(counts)}")

    if not {"9101", "9103"}.issubset({key for key in counts if key.isdigit()}):
        raise AssertionError(f"Healthy API backends did not continue: {dict(counts)}")

    print("✅ Unhealthy backend skipped")

    start_demo_backends([9102])
    wait_port_health("/api", "9102", True)
    verify_random("/api", {"9101", "9102", "9103"})

    print("✅ Backend recovery returned to rotation")


def monitor_8090(stop_event: threading.Event, statuses: list[int | None]) -> None:
    while not stop_event.is_set():
        status, _body = http_request(f"{LIVE_URL}/", timeout=5)
        statuses.append(status)
        time.sleep(1)


def run_update(label: str, command: list[str], monitor: bool = False) -> subprocess.CompletedProcess[str] | None:
    if not monitor:
        return run(command, timeout=1800)

    statuses: list[int | None] = []
    stop_event = threading.Event()
    thread = threading.Thread(target=monitor_8090, args=(stop_event, statuses), daemon=True)

    print(f"🔎 Monitoring 8090 during {label}")
    thread.start()

    try:
        run(command, timeout=1800)
    finally:
        stop_event.set()
        thread.join(timeout=10)

    bad = [status for status in statuses if status != 200]
    print("Continuity statuses:", statuses)

    if bad:
        raise AssertionError(f"8090 had non-200 during {label}: {bad}")

    print(f"✅ Zero-downtime continuity passed for {label}")
    return None


def add_route(runtime: str, strategy: str, route: str, backend: str, monitor: bool) -> subprocess.CompletedProcess[str] | None:
    return run_update(
        f"add {route}",
        [
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
        ],
        monitor=monitor,
    )


def update_prompt(runtime: str, strategy: str, prompt: str, monitor: bool) -> None:
    run_update(
        prompt,
        [
            "pingora-ai",
            "update",
            "--runtime",
            runtime,
            "--strategy",
            strategy,
            "--verify",
            "full",
            f"{prompt} CONFIRM ROUTE MUTATION",
        ],
        monitor=monitor,
    )


def homepage_contains(path: str) -> bool:
    status, body = http_request(f"{LIVE_URL}/")
    return status == 200 and path in body


def verify_route_drift(expected: dict[str, str]) -> None:
    routes = route_map()

    for path, algorithm in expected.items():
        if path not in routes:
            raise AssertionError(f"Route disappeared after update: {path}")

        actual = route_algorithm(routes[path])
        if actual != algorithm:
            raise AssertionError(f"Route drift for {path}: expected {algorithm}, got {actual}")

    print("✅ Route algorithm drift guard passed")


def verify_mutation_integrity(runtime: str, strategy: str, monitor: bool) -> None:
    section(f"Mutation integrity: {runtime}/{strategy}")

    expected = dict(BASELINE_EXPECTED_ALGORITHMS)

    start_demo_backends([9499])

    add_route(runtime, strategy, "/algocheck", "127.0.0.1:9499", monitor=monitor)

    if not homepage_contains("/algocheck"):
        raise AssertionError("/algocheck missing after add")

    verify_single("/algocheck", "9499")
    verify_route_drift(expected)

    duplicate = add_route(runtime, strategy, "/algocheck", "127.0.0.1:9499", monitor=False)
    if duplicate:
        output = f"{duplicate.stdout}\n{duplicate.stderr}".lower()
        markers = [
            "already exists",
            "no deployment needed",
            '"deployed": false',
            '"changed": false',
            "no effective config changes detected",
            "no changes were deployed",
        ]
        if not any(marker in output for marker in markers):
            raise AssertionError(f"Duplicate add did not no-op: {output[-2000:]}")
    print("✅ Duplicate add no-op verified")

    update_prompt(runtime, strategy, "set /api algorithm to round_robin", monitor=monitor)
    expected["/api"] = "round_robin"
    verify_route_drift(expected)
    verify_round_robin("/api", {"9101", "9102", "9103"})

    update_prompt(runtime, strategy, "set /api algorithm to random", monitor=monitor)
    expected["/api"] = "random"
    verify_route_drift(expected)
    verify_random("/api", {"9101", "9102", "9103"})

    update_prompt(runtime, strategy, "remove /algocheck", monitor=monitor)

    if homepage_contains("/algocheck"):
        raise AssertionError("/algocheck still present after remove")

    verify_route_drift(expected)
    verify_all_live_algorithms()

    print("✅ Add/edit/remove algorithm integrity passed")


def run_for_runtime(runtime: str, strategy: str, monitor_updates: bool) -> None:
    section(f"Runtime algorithm integrity: {runtime}/{strategy}")

    cleanup_runtime_keep_fastapi()

    if runtime == "docker_host":
        ensure_docker()

    start_demo_backends(ALL_BACKEND_PORTS)
    apply_gateway(runtime, strategy)
    verify_all_live_algorithms()
    verify_health_failover()
    verify_mutation_integrity(runtime, strategy, monitor=monitor_updates)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--runtime",
        choices=["local", "docker_host", "both"],
        default="both",
    )
    args = parser.parse_args()

    section("Preflight")
    ensure_fastapi()

    if args.runtime in {"local", "both"}:
        run_for_runtime("local", "direct", monitor_updates=False)

    if args.runtime in {"docker_host", "both"}:
        run_for_runtime("docker_host", "bluegreen", monitor_updates=True)

    print("")
    print("✅ FULL ALGORITHM INTEGRITY STRESS TEST PASSED")
    print("✅ random")
    print("✅ round_robin")
    print("✅ weighted_round_robin")
    print("✅ ip_hash")
    print("✅ least_connections")
    print("✅ single backend")
    print("✅ health failover/recovery")
    print("✅ add/edit/remove without algorithm drift")
    print("✅ local/Docker runtime-specific addressing")
    print("✅ Docker zero-downtime update continuity")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
