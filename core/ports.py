from __future__ import annotations

from dataclasses import dataclass


LOCAL_DEV_PORT = 8080

PUBLIC_HTTP_PORT = 80
PUBLIC_HTTPS_PORT = 443

LEGACY_LOCAL_PORT = 8090
LEGACY_DOCKER_HTTPS_PORT = 8443


@dataclass(frozen=True)
class PortPolicy:
    runtime: str
    http_port: int
    https_port: int | None
    live_url: str
    legacy: bool = False


def normalize_runtime(runtime: str | None) -> str:
    value = str(runtime or "local").strip().lower()

    if value in {"docker", "docker_host"}:
        return "docker_host"

    if value in {"linux", "bare_metal", "bare_metal_linux", "systemd"}:
        return "bare_metal_linux"

    if value in {"local", "direct"}:
        return "local"

    return value


def port_policy_for_runtime(runtime: str | None, *, https: bool = False) -> PortPolicy:
    rt = normalize_runtime(runtime)

    if rt == "local":
        return PortPolicy(
            runtime=rt,
            http_port=LOCAL_DEV_PORT,
            https_port=None,
            live_url=f"http://127.0.0.1:{LOCAL_DEV_PORT}",
        )

    if https:
        return PortPolicy(
            runtime=rt,
            http_port=PUBLIC_HTTP_PORT,
            https_port=PUBLIC_HTTPS_PORT,
            live_url=f"https://127.0.0.1:{PUBLIC_HTTPS_PORT}",
        )

    return PortPolicy(
        runtime=rt,
        http_port=PUBLIC_HTTP_PORT,
        https_port=PUBLIC_HTTPS_PORT,
        live_url=f"http://127.0.0.1:{PUBLIC_HTTP_PORT}",
    )


def is_legacy_port(port: int | str | None) -> bool:
    try:
        value = int(port)
    except Exception:
        return False

    return value in {LEGACY_LOCAL_PORT, LEGACY_DOCKER_HTTPS_PORT}
