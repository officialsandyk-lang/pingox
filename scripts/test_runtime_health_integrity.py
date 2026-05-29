from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "generated-pingora-proxy" / "config.json"
LIVE_URL = "http://127.0.0.1:8090"


def run(command: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def normalise_backend(value: Any) -> str:
    text = str(value or "").strip()
    text = text.replace("http://", "").replace("https://", "").rstrip("/")
    text = text.replace("localhost:", "127.0.0.1:")
    text = text.replace("host.docker.internal:", "127.0.0.1:")
    return text


def backend_port(value: Any) -> str:
    match = re.search(r":(\d{2,5})\b", normalise_backend(value))
    return match.group(1) if match else ""


def route_upstreams(route: dict[str, Any]) -> list[str]:
    values: list[Any] = []

    if route.get("upstream"):
        values.append(route["upstream"])

    if isinstance(route.get("upstreams"), list):
        values.extend(route["upstreams"])

    result: list[str] = []

    for value in values:
        if isinstance(value, dict):
            address = value.get("upstream") or value.get("address") or value.get("backend")
            if address:
                result.append(normalise_backend(address))
        else:
            result.append(normalise_backend(value))

    return result


def route_contract(config: dict[str, Any]) -> dict[str, Any]:
    contract: dict[str, Any] = {}

    for route in config.get("routes", []):
        if not isinstance(route, dict):
            continue

        path = route.get("path")
        if not path:
            continue

        contract[path] = {
            "balancing": route.get("balancing"),
            "upstreams": sorted(route_upstreams(route)),
            "health_check": route.get("health_check", {}),
        }

    return contract


def contract_hash(config: dict[str, Any]) -> str:
    payload = json.dumps(route_contract(config), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def http_status(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return int(response.status)
    except Exception:
        return 0


def read_json(url: str) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


def sample_route(path: str, requests: int = 60) -> Counter[str]:
    counts: Counter[str] = Counter()

    url = f"{LIVE_URL}{path}/"

    for _ in range(requests):
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                body = response.read().decode("utf-8", errors="replace")

            data = json.loads(body)
            port = str(data.get("backend_port") or "")
            if port:
                counts[port] += 1

        except Exception as exc:
            counts[f"ERROR:{type(exc).__name__}"] += 1

    return counts


def stop_port(port: int) -> None:
    run(
        ["bash", "-lc", f"lsof -tiTCP:{port} -sTCP:LISTEN | xargs -r kill -9"],
        timeout=10,
    )


def start_demo_backends_for_config() -> None:
    script = PROJECT_ROOT / "scripts" / "demo_backends.py"

    if script.exists():
        result = run([sys.executable, str(script), "start", str(CONFIG_PATH)], timeout=90)
        if result.returncode != 0:
            raise RuntimeError(result.stderr or result.stdout)
        return

    compat = PROJECT_ROOT / "tmp" / "compat_demo_backends.py"
    if compat.exists():
        config = load_config()
        ports = sorted(
            {
                int(port)
                for route in config.get("routes", [])
                for upstream in route_upstreams(route)
                for port in [backend_port(upstream)]
                if port
            }
        )
        subprocess.Popen(
            [sys.executable, str(compat)] + [str(p) for p in ports],
            cwd=str(PROJECT_ROOT),
        )
        time.sleep(3)
        return

    raise RuntimeError("No demo backend starter found.")


def wait_until(condition, timeout_seconds: int, label: str) -> None:
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        if condition():
            return
        time.sleep(1)

    raise AssertionError(f"Timed out waiting for: {label}")


def main() -> int:
    config_before = load_config()
    before_hash = contract_hash(config_before)

    route = "/api"
    expected_ports = {"9101", "9102", "9103"}
    unhealthy_port = "9102"

    print("Runtime health integrity check")
    print(f"Config: {CONFIG_PATH}")
    print(f"Route contract hash before: {before_hash}")

    assert http_status(f"{LIVE_URL}/") in {200, 301, 302, 403, 404, 405}, "gateway is not live"

    before_counts = sample_route(route, 60)
    print("Before failure counts:", dict(before_counts))

    assert expected_ports.issubset(set(before_counts)), (
        f"Expected all ports before failure. Got {dict(before_counts)}"
    )

    print(f"Stopping backend {unhealthy_port}...")
    stop_port(int(unhealthy_port))

    print("Waiting for health checker to mark backend unhealthy...")
    time.sleep(15)

    degraded_counts = sample_route(route, 60)
    print("After failure counts:", dict(degraded_counts))

    assert unhealthy_port not in degraded_counts, (
        f"Unhealthy backend {unhealthy_port} still received traffic: {dict(degraded_counts)}"
    )

    assert {"9101", "9103"}.issubset(set(degraded_counts)), (
        f"Healthy backends did not continue serving: {dict(degraded_counts)}"
    )

    state = read_json(f"{LIVE_URL}/__gateway_runtime_state")
    if state is not None:
        print("Runtime state endpoint available:")
        print(json.dumps(state, indent=2)[:2000])
    else:
        print("Runtime state endpoint not available yet; skipping endpoint assertion.")

    print("Restarting demo backends...")
    start_demo_backends_for_config()

    print("Waiting for backend recovery...")
    time.sleep(15)

    recovered_counts = sample_route(route, 90)
    print("After recovery counts:", dict(recovered_counts))

    assert unhealthy_port in recovered_counts, (
        f"Recovered backend {unhealthy_port} did not return to rotation: {dict(recovered_counts)}"
    )

    config_after = load_config()
    after_hash = contract_hash(config_after)

    print(f"Route contract hash after:  {after_hash}")

    assert before_hash == after_hash, (
        "Route integrity changed during health check test. "
        f"before={before_hash}, after={after_hash}"
    )

    print("")
    print("✅ Runtime upstream health integrity passed")
    print("✅ unhealthy upstream removed from rotation")
    print("✅ healthy upstreams continued serving")
    print("✅ recovered upstream returned to rotation")
    print("✅ route/algorithm/upstream config integrity preserved")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())