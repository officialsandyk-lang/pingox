from __future__ import annotations

import json
from pathlib import Path

from core.ports import (
    LOCAL_DEV_PORT,
    PUBLIC_HTTP_PORT,
    PUBLIC_HTTPS_PORT,
    LEGACY_LOCAL_PORT,
    LEGACY_DOCKER_HTTPS_PORT,
    port_policy_for_runtime,
)
from core.platforms.local import LocalPlatformAdapter
from core.platforms.docker_host import DockerHostPlatformAdapter
from core.platforms.linux_systemd import LinuxSystemdPlatformAdapter
from core.pingora_edge_writer import build_pingora_edge_config


ROOT = Path(__file__).resolve().parents[1]


def assert_ok(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"✅ {name}")


def main() -> int:
    print("\nPingora edge foundation sanity check\n")

    assert_ok("local dev port is 8080", LOCAL_DEV_PORT == 8080)
    assert_ok("public HTTP port is 80", PUBLIC_HTTP_PORT == 80)
    assert_ok("public HTTPS port is 443", PUBLIC_HTTPS_PORT == 443)
    assert_ok("legacy local port is 8090", LEGACY_LOCAL_PORT == 8090)
    assert_ok("legacy Docker HTTPS port is 8443", LEGACY_DOCKER_HTTPS_PORT == 8443)

    local_policy = port_policy_for_runtime("local")
    docker_policy = port_policy_for_runtime("docker_host", https=True)
    linux_policy = port_policy_for_runtime("bare_metal_linux", https=True)

    assert_ok("local live URL uses 8080", local_policy.live_url == "http://127.0.0.1:8080")
    assert_ok("docker live URL uses 443", docker_policy.live_url == "https://127.0.0.1:443")
    assert_ok("linux live URL uses 443", linux_policy.live_url == "https://127.0.0.1:443")

    base = {
        "routes": [
            {"path": "/", "type": "static"},
            {
                "path": "/api",
                "type": "proxy",
                "upstreams": ["127.0.0.1:9101"],
                "balancing": "single",
            },
        ],
        "security": {
            "blocked_paths": ["/.env", "/.git"],
            "allowed_methods": ["GET", "POST"],
        },
    }

    local_cfg = LocalPlatformAdapter().prepare(dict(base))
    docker_cfg = DockerHostPlatformAdapter().prepare(dict(base))
    linux_cfg = LinuxSystemdPlatformAdapter().prepare(dict(base))

    assert_ok("local adapter uses pingora edge", local_cfg["edge"]["type"] == "pingora")
    assert_ok("docker adapter uses pingora edge", docker_cfg["edge"]["type"] == "pingora")
    assert_ok("linux adapter uses pingora edge", linux_cfg["edge"]["type"] == "pingora")

    assert_ok("local adapter listen port 8080", local_cfg["listen_port"] == 8080)
    assert_ok("docker adapter listen port 80", docker_cfg["listen_port"] == 80)
    assert_ok("docker adapter HTTPS port 443", docker_cfg["https_port"] == 443)
    assert_ok("linux adapter listen port 80", linux_cfg["listen_port"] == 80)
    assert_ok("linux adapter HTTPS port 443", linux_cfg["https_port"] == 443)

    edge_config = build_pingora_edge_config(docker_cfg)
    assert_ok("edge config type pingora", edge_config["edge"]["type"] == "pingora")
    assert_ok("edge config HTTP 80", edge_config["listen"]["http"] == 80)
    assert_ok("edge config HTTPS 443", edge_config["listen"]["https"] == 443)
    assert_ok("edge config keeps routes", len(edge_config["routes"]) == 2)

    assert_ok("legacy writer still exists", (ROOT / "core" / "edge_router_writer.py").exists())
    assert_ok("legacy runner still exists", (ROOT / "core" / "edge_router_runner.py").exists())
    assert_ok("legacy nginx config still exists", (ROOT / "docker-bluegreen" / "nginx.conf").exists())

    print("\n✅ PINGORA EDGE FOUNDATION SANITY CHECK PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
