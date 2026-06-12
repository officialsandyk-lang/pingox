from pathlib import Path


def assert_ok(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"✅ {name}")


def main() -> int:
    root = Path("docker-bluegreen")

    config_path = root / "pingora-edge-config.json"
    dockerfile_path = root / "Dockerfile.pingora-edge"
    cargo_path = root / "pingora-edge" / "Cargo.toml"
    main_path = root / "pingora-edge" / "src" / "main.rs"

    assert_ok("Pingora edge config exists", config_path.exists())
    assert_ok("Pingora edge Dockerfile exists", dockerfile_path.exists())
    assert_ok("Pingora edge Cargo.toml exists", cargo_path.exists())
    assert_ok("Pingora edge main.rs exists", main_path.exists())

    config = config_path.read_text(encoding="utf-8")
    dockerfile = dockerfile_path.read_text(encoding="utf-8")
    cargo = cargo_path.read_text(encoding="utf-8")
    main_rs = main_path.read_text(encoding="utf-8")

    assert_ok("config edge type is pingora", '"type": "pingora"' in config)
    assert_ok("config HTTP port is 80", '"http_port": 80' in config)
    assert_ok("config HTTPS port is 443", '"https_port": 443' in config)
    assert_ok("Dockerfile exposes 80 and 443", "EXPOSE 80 443" in dockerfile)
    assert_ok("Dockerfile does not install nginx", "nginx" not in dockerfile.lower())
    assert_ok("Dockerfile installs build tools", "cmake" in dockerfile and "g++" in dockerfile)
    assert_ok("Cargo uses pingora", 'pingora = "0.8"' in cargo)
    assert_ok("Cargo uses pingora-proxy", 'pingora-proxy = "0.8"' in cargo)
    assert_ok("Cargo uses pingora-http", 'pingora-http = "0.8"' in cargo)
    assert_ok("Rust edge imports http_proxy_service", "http_proxy_service" in main_rs)
    assert_ok("Rust edge uses Pingora", "pingora" in main_rs.lower())
    assert_ok("Rust edge forwards to gateway-active", "gateway-active" in main_rs)
    assert_ok("Rust edge marks X-Pingora-Edge", "X-Pingora-Edge" in main_rs)
    assert_ok("legacy nginx config still exists", (root / "nginx.conf").exists())

    print("\n✅ DOCKER PINGORA EDGE FILE SANITY CHECK PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
