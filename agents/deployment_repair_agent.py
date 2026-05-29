from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


try:
    from langsmith import traceable
except Exception:
    def traceable(*args, **kwargs):
        def decorator(fn):
            return fn
        return decorator


TRANSIENT_DOCKER_PATTERNS = [
    r"failed to do request",
    r"EOF",
    r"TLS handshake timeout",
    r"i/o timeout",
    r"connection reset",
    r"temporary failure",
    r"network is unreachable",
    r"failed to resolve source metadata",
    r"failed to copy",
    r"unexpected status",
    r"toomanyrequests",
]


BASE_IMAGE_RE = re.compile(r"FROM\s+([^\s]+)", re.IGNORECASE)


@dataclass
class CommandResult:
    ok: bool
    returncode: int
    stdout: str
    stderr: str
    attempts: int
    repaired: bool
    classification: str


def _run_command(
    command: Sequence[str],
    cwd: str | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.setdefault("DOCKER_BUILDKIT", "1")

    return subprocess.run(
        list(command),
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def _combined_output(result: subprocess.CompletedProcess) -> str:
    return f"{result.stdout or ''}\n{result.stderr or ''}".strip()


def _contains_any(text: str, patterns: list[str]) -> bool:
    lowered = text.lower()
    return any(pattern.lower() in lowered for pattern in patterns)


def _is_transient_docker_failure(output: str) -> bool:
    lowered = output.lower()

    return any(
        re.search(pattern, lowered, re.IGNORECASE)
        for pattern in TRANSIENT_DOCKER_PATTERNS
    )


def _extract_base_images_from_dockerfile(dockerfile_path: str | Path) -> list[str]:
    path = Path(dockerfile_path)

    if not path.exists():
        return []

    content = path.read_text(encoding="utf-8", errors="replace")

    images: list[str] = []

    for match in BASE_IMAGE_RE.finditer(content):
        image = match.group(1).strip()

        if image and image not in images:
            images.append(image)

    return images


@traceable(name="deployment_repair_pull_base_images", run_type="tool")
def pull_base_images(project_dir: str | Path) -> list[str]:
    project_path = Path(project_dir)
    dockerfile_path = project_path / "Dockerfile"
    images = _extract_base_images_from_dockerfile(dockerfile_path)

    pulled: list[str] = []

    for image in images:
        result = _run_command(["docker", "pull", image], cwd=str(project_path))

        if result.returncode == 0:
            pulled.append(image)

    return pulled


def classify_docker_deployment_failure(output: str) -> str:
    text = output or ""
    lower = text.lower()

    if "feature `edition2024` is required" in lower:
        return "docker_rust_toolchain_too_old"

    if (
        "is `cmake` not installed" in lower
        or "cmake = none" in lower
        or "failed to run custom build command for `libz-ng-sys" in lower
    ):
        return "docker_missing_native_build_tools"

    if _is_transient_docker_failure(text):
        return "transient_docker_registry_or_network_failure"

    if "host.docker.internal" in lower and (
        "could not resolve" in lower
        or "name or service not known" in lower
        or "failed to lookup" in lower
    ):
        return "host_docker_internal_unreachable"

    if "dockerfile" in lower and "no such file" in lower:
        return "missing_dockerfile"

    if "compose file" in lower and "no such file" in lower:
        return "missing_compose_artifact"

    return "non_repairable_docker_failure"


def ensure_dockerfile_has_build_tools(project_dir: str | Path) -> bool:
    project_path = Path(project_dir)
    dockerfile = project_path / "Dockerfile"

    if not dockerfile.exists():
        return False

    original = dockerfile.read_text(encoding="utf-8", errors="replace")
    text = original

    # Rust 1.85+ is needed for Rust 2024-edition dependencies.
    text = re.sub(
        r"FROM\s+rust:1\.8[0-4]([^\s]*)",
        r"FROM rust:1.85\1",
        text,
        flags=re.IGNORECASE,
    )

    if "FROM rust:1.85" not in text and "FROM rust:" in text:
        text = re.sub(
            r"FROM\s+rust:[^\s]+",
            "FROM rust:1.85-bookworm",
            text,
            count=1,
            flags=re.IGNORECASE,
        )

    install_block = """RUN apt-get update \\
    && apt-get install -y --no-install-recommends \\
        build-essential \\
        cmake \\
        pkg-config \\
        libssl-dev \\
        ca-certificates \\
    && rm -rf /var/lib/apt/lists/*
"""

    if "apt-get install" not in text or "cmake" not in text:
        lines = text.splitlines()
        output: list[str] = []
        inserted = False

        for line in lines:
            output.append(line)

            if not inserted and line.strip().lower().startswith("from "):
                output.append("")
                output.append(install_block.rstrip())
                inserted = True

        text = "\n".join(output) + "\n"

    if text != original:
        dockerfile.write_text(text, encoding="utf-8")
        return True

    return False


def ensure_dockerignore(project_dir: str | Path) -> bool:
    project_path = Path(project_dir)
    dockerignore = project_path / ".dockerignore"

    desired = """target
.runtime
tmp
*.log
*.sqlite3
*.db
.git
.gitignore
__pycache__
.pytest_cache
*.bak*
*.pyc
data
logs.jsonl
generated-projects
"""

    old = dockerignore.read_text(encoding="utf-8", errors="replace") if dockerignore.exists() else ""

    if old != desired:
        dockerignore.write_text(desired, encoding="utf-8")
        return True

    return False


@traceable(name="deployment_repair_apply_docker_repair", run_type="tool")
def repair_docker_project(
    project_dir: str | Path,
    output: str,
) -> tuple[bool, str]:
    classification = classify_docker_deployment_failure(output)
    changed = False

    if classification in {
        "docker_rust_toolchain_too_old",
        "docker_missing_native_build_tools",
    }:
        changed = ensure_dockerfile_has_build_tools(project_dir) or changed
        changed = ensure_dockerignore(project_dir) or changed
        return changed, classification

    if classification == "transient_docker_registry_or_network_failure":
        pull_base_images(project_dir)
        return True, classification

    return False, classification


@traceable(name="deployment_repair_agent", run_type="chain")
def run_docker_command_with_repair(
    command: Sequence[str],
    project_dir: str,
    max_attempts: int = 3,
    retry_delay_seconds: int = 5,
    timeout: int | None = 1200,
) -> CommandResult:
    repaired = False
    last_result: subprocess.CompletedProcess | None = None
    classification = "unknown"

    for attempt in range(1, max_attempts + 1):
        result = _run_command(command, cwd=project_dir, timeout=timeout)
        last_result = result

        if result.returncode == 0:
            return CommandResult(
                ok=True,
                returncode=0,
                stdout=result.stdout or "",
                stderr=result.stderr or "",
                attempts=attempt,
                repaired=repaired,
                classification="success",
            )

        output = _combined_output(result)
        changed, classification = repair_docker_project(project_dir, output)

        print()
        print("🛠️ Deployment repair agent")
        print(f"Classification: {classification}")
        print(f"Attempt: {attempt}/{max_attempts}")
        print(f"Repair applied: {changed}")

        if changed and attempt < max_attempts:
            repaired = True
            time.sleep(retry_delay_seconds)
            continue

        if classification == "transient_docker_registry_or_network_failure" and attempt < max_attempts:
            repaired = True
            time.sleep(retry_delay_seconds)
            continue

        break

    assert last_result is not None

    return CommandResult(
        ok=False,
        returncode=last_result.returncode,
        stdout=last_result.stdout or "",
        stderr=last_result.stderr or "",
        attempts=max_attempts,
        repaired=repaired,
        classification=classification,
    )
