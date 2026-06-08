from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


SAFE_TESTS = {
    "decision": [
        [sys.executable, "scripts/audit_decision_integrity_and_learning.py"],
    ],
    "edge": [
        [sys.executable, "scripts/sanity_pingora_edge_foundation.py"],
        [sys.executable, "scripts/sanity_docker_pingora_edge_files.py"],
    ],
    "linux": [
        [sys.executable, "scripts/sanity_linux_systemd_pingora_only.py"],
    ],
}

LIVE_DOCKER_TESTS = {
    "docker": [
        [sys.executable, "scripts/sanity_pingora_edge_foundation.py"],
        [sys.executable, "scripts/sanity_docker_pingora_edge_files.py"],
    ],
}

LIVE_LINUX_TESTS = {
    "linux-live": [
        [sys.executable, "scripts/sanity_linux_systemd_pingora_only.py"],
    ],
}


def _script_exists(command: list[str]) -> bool:
    if len(command) < 2:
        return True
    script = PROJECT_ROOT / command[1]
    return script.exists()


def _run(command: list[str]) -> int:
    if not _script_exists(command):
        print(f"⚠️ Skipping missing script: {command[1]}")
        return 0

    print("")
    print("▶️", " ".join(command))
    result = subprocess.run(command, cwd=PROJECT_ROOT)

    if result.returncode != 0:
        print(f"❌ Test failed: {' '.join(command)}")
        return result.returncode

    return 0


def run_test_group(
    group: str,
    *,
    with_live_docker: bool = False,
    with_live_linux: bool = False,
) -> int:
    commands: list[list[str]] = []

    if group == "all":
        for name in ["edge", "linux", "decision"]:
            commands.extend(SAFE_TESTS.get(name, []))
    elif group in SAFE_TESTS:
        commands.extend(SAFE_TESTS[group])
    elif group == "docker":
        if not with_live_docker:
            print("⚠️ Docker live tests require --with-live-docker.")
            print("No Docker deployment was touched.")
            return 2
        commands.extend(LIVE_DOCKER_TESTS["docker"])
    elif group == "linux-live":
        if not with_live_linux:
            print("⚠️ Linux live tests require --with-live-linux.")
            print("No remote Linux deployment was touched.")
            return 2
        commands.extend(LIVE_LINUX_TESTS["linux-live"])
    else:
        print(f"Unknown test group: {group}")
        print("Available: decision, edge, linux, docker, linux-live, all")
        return 2

    for command in commands:
        code = _run(command)
        if code != 0:
            return code

    print("")
    print(f"✅ pingora-ai test {group} passed")
    return 0


def run_doctor() -> int:
    checks = [
        [sys.executable, "-m", "py_compile", "pingora_ai/cli.py"],
        [sys.executable, "-m", "py_compile", "core/decision_integrity.py"],
        [sys.executable, "-m", "py_compile", "core/backup_registry.py"],
    ]

    for command in checks:
        code = _run(command)
        if code != 0:
            return code

    print("")
    print("✅ pingora-ai doctor passed")
    return 0
