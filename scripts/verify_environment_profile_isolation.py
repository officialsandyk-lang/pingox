from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "generated-pingora-proxy"

LOCAL_CONFIG = PROJECT / "config.local.json"
DOCKER_CONFIG = PROJECT / "config.docker.json"
LINUX_CONFIG = PROJECT / "config.linux.json"
BASE_CONFIG = PROJECT / "config.json"
ACTIVE_CONTEXT = ROOT / "data" / "active_runtime_context.json"


def fail(message: str) -> None:
    print(f"❌ {message}")
    raise SystemExit(1)


def ok(message: str) -> None:
    print(f"✅ {message}")


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        fail(f"Missing required file: {path}")

    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        fail(f"Invalid JSON in {path}: {exc}")

    if not isinstance(data, dict):
        fail(f"Expected JSON object in {path}")

    return data


def sha(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )


def route_addresses(config: dict[str, Any]) -> list[str]:
    addresses: list[str] = []

    for route in config.get("routes") or []:
        if not isinstance(route, dict):
            continue

        upstreams = route.get("upstreams")
        if upstreams is None and route.get("upstream"):
            upstreams = [route.get("upstream")]

        for item in upstreams or []:
            if isinstance(item, dict):
                address = item.get("address") or item.get("upstream") or item.get("backend")
            else:
                address = item

            if address:
                addresses.append(str(address))

    return addresses


def assert_profile_content() -> None:
    local = load_json(LOCAL_CONFIG)
    docker = load_json(DOCKER_CONFIG)

    local_text = LOCAL_CONFIG.read_text()
    docker_text = DOCKER_CONFIG.read_text()

    if "host.docker.internal" in local_text:
        fail("config.local.json contains host.docker.internal")

    if local.get("runtime") != "local":
        fail(f"config.local.json runtime must be local, got {local.get('runtime')!r}")

    if docker.get("runtime") != "docker_host":
        fail(f"config.docker.json runtime must be docker_host, got {docker.get('runtime')!r}")

    if "host.docker.internal" not in docker_text:
        fail("config.docker.json does not contain host.docker.internal")

    local_addresses = route_addresses(local)
    docker_addresses = route_addresses(docker)

    if any("host.docker.internal" in address for address in local_addresses):
        fail("local route upstreams leaked Docker host addressing")

    if docker_addresses and not any("host.docker.internal" in address for address in docker_addresses):
        fail("docker route upstreams do not show Docker host addressing")

    ok("Local config uses local runtime/addressing only")
    ok("Docker config uses docker_host runtime/addressing only")


def assert_command_does_not_mutate_other_profiles() -> None:
    before = {
        "local": sha(LOCAL_CONFIG),
        "docker": sha(DOCKER_CONFIG),
        "linux": sha(LINUX_CONFIG),
        "base": sha(BASE_CONFIG),
    }

    local_status = run(["pingora-ai", "status", "--local"], timeout=120)
    if local_status.returncode != 0:
        fail(f"pingora-ai status --local failed:\n{local_status.stdout}")

    after_local_status = {
        "local": sha(LOCAL_CONFIG),
        "docker": sha(DOCKER_CONFIG),
        "linux": sha(LINUX_CONFIG),
        "base": sha(BASE_CONFIG),
    }

    if before["docker"] != after_local_status["docker"]:
        fail("Docker config changed during local status command")

    if before["linux"] != after_local_status["linux"]:
        fail("Linux config changed during local status command")

    ok("Local status command did not mutate Docker/Linux configs")

    docker_status = run(["pingora-ai", "status", "--docker"], timeout=120)
    if docker_status.returncode != 0:
        fail(f"pingora-ai status --docker failed:\n{docker_status.stdout}")

    after_docker_status = {
        "local": sha(LOCAL_CONFIG),
        "docker": sha(DOCKER_CONFIG),
        "linux": sha(LINUX_CONFIG),
        "base": sha(BASE_CONFIG),
    }

    if after_local_status["local"] != after_docker_status["local"]:
        fail("Local config changed during Docker status command")

    if after_local_status["linux"] != after_docker_status["linux"]:
        fail("Linux config changed during Docker status command")

    ok("Docker status command did not mutate Local/Linux configs")


def assert_active_context() -> None:
    context = load_json(ACTIVE_CONTEXT)

    runtime_context = context.get("runtime_context")
    address_mode = context.get("address_mode")
    runtime = context.get("runtime")
    base_url = context.get("base_url")
    active_config_path = str(context.get("active_config_path") or "")

    if runtime_context == "local":
        if address_mode != "local":
            fail(f"Active local context has wrong address_mode: {address_mode!r}")
        if runtime != "local":
            fail(f"Active local context has wrong runtime: {runtime!r}")
        if base_url != "http://127.0.0.1:8080":
            fail(f"Active local context has wrong base_url: {base_url!r}")
        if not active_config_path.endswith("generated-pingora-proxy/config.local.json"):
            fail(f"Active local context points to wrong config: {active_config_path}")

        ok("Active runtime context correctly points to local profile")
        return

    if runtime_context in {"docker", "docker_host"}:
        if address_mode not in {"docker", "docker_host"}:
            fail(f"Active Docker context has wrong address_mode: {address_mode!r}")
        if runtime not in {"docker", "docker_host"}:
            fail(f"Active Docker context has wrong runtime: {runtime!r}")
        if "config.docker.json" not in active_config_path:
            fail(f"Active Docker context points to wrong config: {active_config_path}")

        ok("Active runtime context correctly points to Docker profile")
        return

    print(f"⚠️  Active runtime context is {runtime_context!r}; accepted because no runtime switch was requested")


def main() -> int:
    print("🧪 Verifying environment profile isolation")
    print("Scope: local/Docker/Linux config separation; no deploy; status checks only")

    assert_profile_content()
    assert_command_does_not_mutate_other_profiles()
    assert_profile_content()
    assert_active_context()

    print("\n✅ ENVIRONMENT PROFILE ISOLATION VERIFICATION PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
