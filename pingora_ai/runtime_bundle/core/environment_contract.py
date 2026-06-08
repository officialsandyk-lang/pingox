from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any


DOCKER_RUNTIMES = {"docker", "docker_host", "container", "compose"}


def _run(command: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def docker_available() -> tuple[bool, str]:
    if not shutil.which("docker"):
        return False, "Docker CLI not found in this WSL distro."

    version = _run(["docker", "--version"])

    if version.returncode != 0:
        return False, version.stderr or version.stdout or "docker --version failed"

    ps = _run(["docker", "ps"])

    if ps.returncode != 0:
        return False, ps.stderr or ps.stdout or "docker ps failed"

    return True, version.stdout.strip()


def require_docker_available() -> None:
    ok, detail = docker_available()

    if not ok:
        raise RuntimeError(
            "Docker runtime requested, but Docker is not available.\n"
            f"Reason: {detail}\n\n"
            "Enable Docker Desktop WSL integration, then retry."
        )


def validate_requested_environment(
    *,
    runtime_mode: str,
    strategy: str,
    project_root: str | Path,
) -> None:
    runtime_mode = str(runtime_mode or "local").strip().lower()
    strategy = str(strategy or "direct").strip().lower()

    if runtime_mode in DOCKER_RUNTIMES or strategy == "bluegreen":
        require_docker_available()

    if strategy == "bluegreen" and runtime_mode == "local":
        raise RuntimeError(
            "Invalid environment contract: bluegreen deployment cannot run as local/direct. "
            "Use runtime_mode=docker_host for Docker/bluegreen."
        )


def _result_runtime_strategy(result: dict[str, Any]) -> tuple[str, str]:
    config = result.get("config") if isinstance(result, dict) else None

    if not isinstance(config, dict):
        config = {}

    runtime = (
        result.get("runtime")
        or result.get("runtime_mode")
        or config.get("runtime")
        or config.get("runtime_mode")
        or ""
    )

    strategy = (
        result.get("strategy")
        or result.get("deployment_strategy")
        or config.get("strategy")
        or config.get("deployment_strategy")
        or ""
    )

    return str(runtime).strip().lower(), str(strategy).strip().lower()


def assert_graph_did_not_fallback(
    *,
    result: dict[str, Any],
    requested_runtime_mode: str,
    requested_strategy: str,
) -> None:
    requested_runtime_mode = str(requested_runtime_mode or "").strip().lower()
    requested_strategy = str(requested_strategy or "").strip().lower()

    actual_runtime, actual_strategy = _result_runtime_strategy(result)

    if requested_runtime_mode in DOCKER_RUNTIMES:
        if actual_runtime == "local":
            raise RuntimeError(
                "Environment mismatch: user requested Docker runtime, "
                f"but graph returned runtime={actual_runtime}, strategy={actual_strategy}. "
                "Refusing to trust a local/direct fallback."
            )

    if requested_strategy == "bluegreen":
        if actual_strategy == "direct":
            raise RuntimeError(
                "Environment mismatch: user requested bluegreen deployment, "
                f"but graph returned strategy={actual_strategy}. "
                "Refusing to trust a local/direct fallback."
            )


def assert_docker_containers_running_for_bluegreen() -> None:
    ps = _run(["docker", "ps", "--format", "{{.Names}}"])

    if ps.returncode != 0:
        raise RuntimeError("Could not inspect Docker containers:\n" + (ps.stderr or ps.stdout))

    names = [line.strip() for line in ps.stdout.splitlines() if line.strip()]

    if not names:
        raise RuntimeError(
            "Docker/bluegreen was requested, but no Docker containers are running after deploy. "
            "This means the system did not actually deploy to Docker."
        )
