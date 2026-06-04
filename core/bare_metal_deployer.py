from __future__ import annotations
from core.homepage_route import ensure_route_directory_homepage

from typing import Any

from core.bare_metal.deployer import (
    BareMetalDeployOptions,
    BareMetalLinuxDeployer,
)


def deploy_bare_metal_linux(
    config: dict[str, Any],
    *,
    host: str,
    user: str = "ubuntu",
    ssh_key: str | None = None,
    ssh_port: int = 22,
    server_name: str = "_",
    public_http_port: int = 80,
    binary_path: str = "generated-pingora-proxy/target/release/generated-pingora-proxy",
    service_user: str = "www-data",
    verify_path: str = "/",
    verify_timeout_seconds: int = 60,
) -> dict[str, Any]:
    """
    Deploy a generated Pingora gateway directly on a Linux server without Docker.

    Flow:
    local CLI -> SSH -> remote Linux -> systemd blue/green -> host NGINX -> verify
    """
    if not isinstance(config, dict):
        raise TypeError("config must be a dictionary")

    if not host:
        raise ValueError("host is required for bare_metal_linux deployment")

    deployer = BareMetalLinuxDeployer(
        BareMetalDeployOptions(
            host=host,
            user=user,
            ssh_key=ssh_key,
            ssh_port=ssh_port,
            server_name=server_name,
            public_http_port=public_http_port,
            binary_path=binary_path,
            service_user=service_user,
            verify_path=verify_path,
            verify_timeout_seconds=verify_timeout_seconds,
        )
    )

    result = deployer.deploy(config)

    return {
        "success": result.success,
        "target": result.target,
        "active_color": result.active_color,
        "active_port": result.active_port,
        "live_url": result.live_url,
        "message": result.message,
        "error": result.error,
    }


def rollback_bare_metal_linux(
    *,
    host: str,
    user: str = "ubuntu",
    ssh_key: str | None = None,
    ssh_port: int = 22,
    server_name: str = "_",
    public_http_port: int = 80,
    verify_path: str = "/",
    verify_timeout_seconds: int = 60,
) -> dict[str, Any]:
    """
    Roll back host NGINX to the previous bare_metal_linux blue/green target.
    """
    if not host:
        raise ValueError("host is required for bare_metal_linux rollback")

    deployer = BareMetalLinuxDeployer(
        BareMetalDeployOptions(
            host=host,
            user=user,
            ssh_key=ssh_key,
            ssh_port=ssh_port,
            server_name=server_name,
            public_http_port=public_http_port,
            verify_path=verify_path,
            verify_timeout_seconds=verify_timeout_seconds,
        )
    )

    result = deployer.rollback()

    return {
        "success": result.success,
        "target": result.target,
        "active_color": result.active_color,
        "active_port": result.active_port,
        "live_url": result.live_url,
        "message": result.message,
        "error": result.error,
    }


def status_bare_metal_linux(
    *,
    host: str,
    user: str = "ubuntu",
    ssh_key: str | None = None,
    ssh_port: int = 22,
    server_name: str = "_",
    public_http_port: int = 80,
) -> dict[str, Any]:
    """
    Return remote bare_metal_linux state and service status.
    """
    if not host:
        raise ValueError("host is required for bare_metal_linux status")

    deployer = BareMetalLinuxDeployer(
        BareMetalDeployOptions(
            host=host,
            user=user,
            ssh_key=ssh_key,
            ssh_port=ssh_port,
            server_name=server_name,
            public_http_port=public_http_port,
        )
    )

    return deployer.status()
