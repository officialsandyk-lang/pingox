from __future__ import annotations

from typing import Any

from core.platforms.base import DeploymentResult
from core.ports import port_policy_for_runtime


class DockerHostPlatformAdapter:
    runtime = "docker_host"

    def prepare(self, config: dict[str, Any]) -> dict[str, Any]:
        policy = port_policy_for_runtime("docker_host", https=True)
        config["runtime"] = "docker_host"
        config["runtime_mode"] = "docker_host"
        config["strategy"] = "bluegreen"
        config["deployment_strategy"] = "bluegreen"
        config["public_port"] = policy.http_port
        config["listen_port"] = policy.http_port
        config["https_port"] = policy.https_port
        config["live_url"] = policy.live_url
        config.setdefault("edge", {})["type"] = "pingora"
        return config

    def deploy(self, config: dict[str, Any]) -> DeploymentResult:
        return DeploymentResult(False, self.runtime, error="Not wired yet.")

    def verify(self, config: dict[str, Any]) -> DeploymentResult:
        return DeploymentResult(False, self.runtime, error="Not wired yet.")

    def rollback(self, config: dict[str, Any]) -> DeploymentResult:
        return DeploymentResult(False, self.runtime, error="Not wired yet.")

    def destroy(self) -> DeploymentResult:
        return DeploymentResult(False, self.runtime, error="Not wired yet.")
