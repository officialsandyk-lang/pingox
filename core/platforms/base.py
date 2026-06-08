from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class DeploymentResult:
    success: bool
    runtime: str
    live_url: str = ""
    active_color: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class PlatformAdapter(Protocol):
    runtime: str

    def prepare(self, config: dict[str, Any]) -> dict[str, Any]:
        ...

    def deploy(self, config: dict[str, Any]) -> DeploymentResult:
        ...

    def verify(self, config: dict[str, Any]) -> DeploymentResult:
        ...

    def rollback(self, config: dict[str, Any]) -> DeploymentResult:
        ...

    def destroy(self) -> DeploymentResult:
        ...
