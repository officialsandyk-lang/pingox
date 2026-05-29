from __future__ import annotations

import json
import os
import re
import subprocess
import sys
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


def run(command: list[str], timeout: int = 900, check: bool = True) -> subprocess.CompletedProcess[str]:
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
        print(result.stdout[-8000:])

    if result.stderr.strip():
        print(result.stderr[-4000:])

    if check and result.returncode != 0:
        raise AssertionError(f"Command failed with exit code {result.returncode}: {' '.join(command)}")

    return result


def http_request(url: str, timeout: int = 10) -> tuple[int | None, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return int(response.status), response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def json_get(url: str) -> dict[str, Any]:
    status, body = http_request(url)

    if status != 200:
        raise AssertionError(f"Expected 200 from {url}, got {status}: {body[:300]}")

    return json.loads(body)


def wait_until(label: str, condition, timeout_seconds: int = 120, interval: float = 2.0):
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
    assert data.get("ok") is True, data
    print("✅ FastAPI is running")


def ensure_docker() -> None:
    if (PROJECT_ROOT / "scripts" / "docker_ready.sh").exists():
        run(["bash", "scripts/docker_ready.sh", "180"], timeout=240)
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


def start_demo_backends(config_path: Path | None = None) -> None:
    if config_path is None:
        config_path = PROJECT_ROOT / "generated-pingora-proxy" / "config.json"

    if not config_path.exists():
        config_path = make_backend_config(
            PROJECT_ROOT / "tmp" / "reset_rollback_baseline_backends.json",
            [9101, 9102, 9103],
        )

    run([PYTHON, "scripts/demo_backends.py", "start", str(config_path)], timeout=120)


def wait_live() -> None:
    wait_until(
        "gateway live on 8090",
        lambda: http_request(f"{LIVE_URL}/")[0] == 200,
        timeout_seconds=180,
    )
    print("✅ Gateway live on 8090")


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
    )
    wait_live()


def config() -> dict[str, Any]:
    path = PROJECT_ROOT / "generated-pingora-proxy" / "config.json"
    return json.loads(path.read_text(encoding="utf-8"))


def route(path: str) -> dict[str, Any] | None:
    for item in config().get("routes", []):
        if item.get("path") == path:
            return item
    return None


def route_algorithm(path: str) -> str | None:
    item = route(path)
    if not item:
        return None
    return item.get("balancing")


def homepage_contains(text: str) -> bool:
    status, body = http_request(f"{LIVE_URL}/")
    return status == 200 and text in body


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


def sample_route(path: str, count: int = 60) -> Counter[str]:
    counts: Counter[str] = Counter()
    url = f"{LIVE_URL}{path.rstrip('/')}/"

    for _ in range(count):
        status, body = http_request(url)

        if status != 200:
            counts[f"HTTP_{status}"] += 1
            continue

        port = extract_backend_port(body)
        if port:
            counts[port] += 1
        else:
            counts["NO_BACKEND"] += 1

    return counts


def assert_api_random() -> None:
    counts = sample_route("/api", 90)
    seen = {key for key in counts if key.isdigit()}

    print("Random counts:", dict(counts))

    if not BASELINE_PORTS.issubset(seen):
        raise AssertionError(f"/api random missing ports. Expected {BASELINE_PORTS}, got {dict(counts)}")

    print("✅ /api random works")


def assert_route_backend(path: str, expected_port: str) -> None:
    counts = sample_route(path, 20)
    print(f"{path} counts:", dict(counts))

    if expected_port not in counts:
        raise AssertionError(f"Expected {path} to use {expected_port}, got {dict(counts)}")

    print(f"✅ {path} routes to {expected_port}")


def runtime_health() -> dict[str, bool]:
    data = json_get(f"{LIVE_URL}/__gateway_runtime_state")
    health: dict[str, bool] = {}

    for route_item in data.get("routes", []):
        if route_item.get("path") != "/api":
            continue

        for upstream in route_item.get("upstreams", []):
            address = str(upstream.get("address") or "")
            match = re.search(r":(\d{2,5})\b", address)
            if match:
                health[match.group(1)] = bool(upstream.get("healthy"))

    return health


def wait_api_healthy() -> None:
    wait_until(
        "/api all upstreams healthy",
        lambda: BASELINE_PORTS.issubset(set(runtime_health())) and all(runtime_health().get(port) for port in BASELINE_PORTS),
        timeout_seconds=120,
    )
    print("✅ /api runtime health all green")


def add_route(path: str, backend: str) -> None:
    run(
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
    )


def edit_api_algorithm(algorithm: str) -> None:
    run(
        [
            "pingora-ai",
            "edit",
            "--runtime",
            "docker_host",
            "--strategy",
            "bluegreen",
            "--verify",
            "full",
            "/api",
            "--algorithm",
            algorithm,
        ],
        timeout=1800,
    )


def rollback_bluegreen() -> None:
    run([PYTHON, "rollback.py", "--mode", "bluegreen", "--verify", "full"], timeout=1800)
    wait_live()


def docker_stack_reset_only() -> None:
    run(
        ["docker", "--context", "desktop-linux", "compose", "-f", "docker-bluegreen/docker-compose.yml", "down", "--remove-orphans"],
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
        ["docker", "--context", "desktop-linux", "network", "rm", "docker-bluegreen_ai_pingora_net"],
        timeout=60,
        check=False,
    )


def test_rollback_add() -> None:
    section("Rollback after add")

    backend_port = 9407
    route_path = "/rollbackprobe"

    backend_config = make_backend_config(
        PROJECT_ROOT / "tmp" / "rollback_probe_backend.json",
        [backend_port],
    )
    start_demo_backends(backend_config)

    add_route(route_path, f"127.0.0.1:{backend_port}")

    if not homepage_contains(route_path):
        raise AssertionError(f"{route_path} missing after add")

    assert_route_backend(route_path, str(backend_port))

    rollback_bluegreen()

    if homepage_contains(route_path) or route(route_path) is not None:
        raise AssertionError(f"{route_path} still exists after rollback")

    wait_api_healthy()
    assert_api_random()

    print("✅ rollback after add works")


def test_rollback_edit() -> None:
    section("Rollback after edit")

    if route_algorithm("/api") != "random":
        apply_baseline()

    edit_api_algorithm("round_robin")

    if route_algorithm("/api") != "round_robin":
        raise AssertionError(f"Expected /api round_robin before rollback, got {route_algorithm('/api')}")

    rollback_bluegreen()

    if route_algorithm("/api") != "random":
        raise AssertionError(f"Expected /api random after rollback, got {route_algorithm('/api')}")

    wait_api_healthy()
    assert_api_random()

    print("✅ rollback after edit works")


def test_reset_and_reapply() -> None:
    section("Docker stack reset and clean re-apply")

    docker_stack_reset_only()

    wait_until(
        "8090 down after Docker reset",
        lambda: http_request(f"{LIVE_URL}/", timeout=3)[0] is None,
        timeout_seconds=60,
        interval=2,
    )

    print("✅ 8090 went down after Docker stack reset")

    start_demo_backends()
    apply_baseline()
    wait_api_healthy()
    assert_api_random()

    print("✅ reset and clean re-apply works")


def main() -> int:
    section("Preflight")
    ensure_fastapi()
    ensure_docker()

    section("Start baseline")
    start_demo_backends()
    apply_baseline()
    wait_api_healthy()
    assert_api_random()

    test_rollback_add()
    test_rollback_edit()
    test_reset_and_reapply()

    print("")
    print("✅ CLI RESET / ROLLBACK STRESS TEST PASSED")
    print("✅ rollback after add")
    print("✅ rollback after edit")
    print("✅ docker stack reset")
    print("✅ clean re-apply after reset")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
