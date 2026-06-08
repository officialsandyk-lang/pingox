from __future__ import annotations

from typing import Any


def validate_runtime_mode_boundary(config: dict[str, Any]) -> None:
    runtime = str(config.get("runtime") or config.get("runtime_mode") or "").strip()

    if runtime == "docker_host":
        if config.get("systemd") or config.get("_systemd"):
            raise RuntimeError("Runtime mode leak blocked: docker_host cannot include systemd flags.")
        if config.get("replace_edge") or config.get("_replace_edge"):
            raise RuntimeError("Runtime mode leak blocked: docker_host cannot include replace_edge flags.")

    if runtime in {"bare_metal_linux", "linux"}:
        if config.get("systemd") or config.get("_systemd"):
            raise RuntimeError("Runtime mode leak blocked: bare_metal_linux cannot include local _systemd flags.")
        if config.get("replace_edge") or config.get("_replace_edge"):
            raise RuntimeError("Runtime mode leak blocked: bare_metal_linux cannot include local replace_edge flags.")
