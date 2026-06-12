from __future__ import annotations

from pathlib import Path

from core.platforms.linux_systemd import LinuxSystemdPlatformAdapter
from core.ports import PUBLIC_HTTP_PORT, PUBLIC_HTTPS_PORT


def assert_ok(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"✅ {name}")


def main() -> int:
    cfg = {
        "routes": [
            {"path": "/", "type": "static"},
            {
                "path": "/api",
                "type": "proxy",
                "upstreams": ["127.0.0.1:9101"],
                "balancing": "single",
            },
        ]
    }

    prepared = LinuxSystemdPlatformAdapter().prepare(cfg)

    assert_ok("runtime is bare_metal_linux", prepared["runtime"] == "bare_metal_linux")
    assert_ok("strategy is systemd", prepared["strategy"] == "systemd")
    assert_ok("listen port is 80", prepared["listen_port"] == PUBLIC_HTTP_PORT)
    assert_ok("https port is 443", prepared["https_port"] == PUBLIC_HTTPS_PORT)
    assert_ok("edge type is pingora", prepared["edge"]["type"] == "pingora")
    assert_ok("edge enabled", prepared["edge"]["enabled"] is True)
    assert_ok("nginx disabled in edge", prepared["edge"]["nginx_enabled"] is False)
    assert_ok("top-level nginx disabled", prepared["nginx_enabled"] is False)
    assert_ok("replace nginx requested", prepared["replace_nginx"] is True)

    systemd_text = Path("core/bare_metal/systemd_writer.py").read_text(encoding="utf-8")
    assert_ok("systemd has CAP_NET_BIND_SERVICE", "AmbientCapabilities=CAP_NET_BIND_SERVICE" in systemd_text)
    assert_ok("systemd bounds CAP_NET_BIND_SERVICE", "CapabilityBoundingSet=CAP_NET_BIND_SERVICE" in systemd_text)
    assert_ok("systemd has NoNewPrivileges", "NoNewPrivileges=true" in systemd_text)
    assert_ok("systemd passes --port", "--port {listen_port}" in systemd_text)

    deployer_text = Path("core/bare_metal/deployer.py").read_text(encoding="utf-8")
    assert_ok("deployer has Pingora-only edge detector", "def _is_pingora_only_edge" in deployer_text)
    assert_ok("deployer uses port 80 for Pingora-only candidate", "return 80" in deployer_text)
    assert_ok("deployer stops nginx in Pingora-only preflight", "systemctl stop --no-block nginx" in deployer_text)
    assert_ok("deploy path skips nginx for Pingora-only", "Pingora owns public port 80 directly. No NGINX config/reload." in deployer_text)
    assert_ok("rollback stops current Pingora service", "current_service = f\"pingora-ai-{state.active_color}\"" in deployer_text)
    assert_ok("rollback restarts previous Pingora service", "previous_service = f\"pingora-ai-{state.previous_color}\"" in deployer_text)
    assert_ok("rollback verifies Pingora-only public port", "_verify_public(config={\"edge\": {\"type\": \"pingora\"}, \"replace_nginx\": True})" in deployer_text)
    assert_ok("rollback live URL uses Pingora-only helper", "self._live_url_for_config({\"edge\": {\"type\": \"pingora\"}, \"replace_nginx\": True})" in deployer_text)

    print("\n✅ LINUX SYSTEMD PINGORA-ONLY SANITY CHECK PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
