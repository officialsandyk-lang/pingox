from __future__ import annotations

from typing import Any

from core.platforms.base import DeploymentResult
from core.ports import port_policy_for_runtime


class LinuxSystemdPlatformAdapter:
    runtime = "bare_metal_linux"

    def prepare(self, config: dict[str, Any]) -> dict[str, Any]:
        policy = port_policy_for_runtime("bare_metal_linux", https=True)
        config["runtime"] = "bare_metal_linux"
        config["runtime_mode"] = "bare_metal_linux"
        config["strategy"] = "systemd"
        config["deployment_strategy"] = "systemd"
        config["public_port"] = policy.http_port
        config["listen_port"] = policy.http_port
        config["https_port"] = policy.https_port
        config["live_url"] = policy.live_url
        edge = config.setdefault("edge", {})
        edge["type"] = "pingora"
        edge["enabled"] = True
        edge["nginx_enabled"] = False

        config["nginx_enabled"] = False
        config["replace_nginx"] = True
        config.setdefault("systemd", True)
        return config

    def deploy(self, config: dict[str, Any]) -> DeploymentResult:
        return DeploymentResult(False, self.runtime, error="Not wired yet.")

    def verify(self, config: dict[str, Any]) -> DeploymentResult:
        return DeploymentResult(False, self.runtime, error="Not wired yet.")

    def rollback(self, config: dict[str, Any]) -> DeploymentResult:
        return DeploymentResult(False, self.runtime, error="Not wired yet.")

    def destroy(self) -> DeploymentResult:
        return DeploymentResult(False, self.runtime, error="Not wired yet.")
