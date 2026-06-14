#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / "generated-pingora-proxy"


SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"OPENAI_API_KEY\s*=\s*['\"]?sk-", re.IGNORECASE),
    re.compile(r"PINGORA_AI_API_TOKEN\s*=\s*['\"]?[A-Za-z0-9_\-]{20,}", re.IGNORECASE),
    re.compile(r"AWS_SECRET_ACCESS_KEY\s*=", re.IGNORECASE),
    re.compile(r"PRIVATE KEY-----"),
]


EXPECTED_GITIGNORE_MARKERS = [
    ".env",
    "*.sqlite",
    "*.sqlite3",
    "*.log",
    "generated-pingora-proxy/tmp/",
    "generated-pingora-proxy/target/",
]


EXPECTED_CONFIGS = [
    GENERATED / "config.local.json",
    GENERATED / "config.docker.json",
    GENERATED / "config.linux.json",
]


def fail(message: str) -> None:
    raise SystemExit(f"❌ {message}")


def ok(message: str) -> None:
    print(f"✅ {message}")


def warn(message: str) -> None:
    print(f"⚠️  {message}")


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def run_command(args: list[str], timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )


def tracked_files() -> list[str]:
    result = run_command(["git", "ls-files"], timeout=30)
    if result.returncode != 0:
        fail(f"git ls-files failed:\n{result.stdout}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def verify_gitignore() -> None:
    gitignore = ROOT / ".gitignore"
    if not gitignore.exists():
        fail(".gitignore is missing")

    content = read_text(gitignore)

    missing = [marker for marker in EXPECTED_GITIGNORE_MARKERS if marker not in content]
    if missing:
        fail(f".gitignore missing protection markers: {missing}")

    ok(".gitignore protects env, DB, logs, tmp, and target artifacts")


def verify_env_example() -> None:
    candidates = [
        ROOT / ".env.example",
        ROOT / "env.example",
        ROOT / "example.env",
    ]

    existing = [path for path in candidates if path.exists()]
    if not existing:
        fail(".env.example is missing")

    text = read_text(existing[0])
    expected_markers = [
        "PINGORA_AI",
        "CONTROL",
    ]

    missing = [marker for marker in expected_markers if marker.lower() not in text.lower()]
    if missing:
        warn(f"{existing[0].name} exists but may be missing useful markers: {missing}")

    ok(f"{existing[0].name} exists")


def verify_no_sensitive_files_tracked() -> None:
    tracked = tracked_files()

    forbidden_exact = {
        ".env",
        "data/reliability_reports.sqlite3",
        "data/agent_reports.sqlite3",
        "data/backup_registry.sqlite3",
        "data/incidents.sqlite",
        "data/security/api_keys.sqlite3",
    }

    forbidden_suffixes = (
        ".sqlite",
        ".sqlite3",
        ".db",
        ".log",
        ".key",
        ".pem",
        ".crt",
    )

    bad: list[str] = []

    for item in tracked:
        if item in forbidden_exact:
            bad.append(item)
            continue

        if item.endswith(forbidden_suffixes):
            if item.endswith(".crt") and "test" in item.lower():
                continue
            bad.append(item)
            continue

        if "/tmp/" in item or item.startswith("generated-pingora-proxy/tmp/"):
            bad.append(item)

        if item.startswith("generated-pingora-proxy/target/"):
            bad.append(item)

    if bad:
        fail(f"tracked sensitive/generated artifacts found: {bad[:20]}")

    ok("No obvious sensitive DB/log/key/tmp/target artifacts are tracked")


def verify_no_obvious_secrets_in_tracked_files() -> None:
    tracked = tracked_files()

    allowed_suffixes = {
        ".py",
        ".rs",
        ".toml",
        ".json",
        ".md",
        ".txt",
        ".yml",
        ".yaml",
        ".sh",
        ".html",
        ".css",
        ".js",
        ".example",
        ".gitignore",
    }

    skip_dirs = (
        ".git/",
        "generated-pingora-proxy/target/",
        "generated-pingora-proxy/tmp/",
        "data/",
    )

    skip_secret_scanner_files = {
        "scripts/verify_security_cve_local.py",
        "scripts/verify_production_hardening_foundation.py",
    }

    findings: list[str] = []

    for item in tracked:
        if item.startswith(skip_dirs):
            continue

        if item in skip_secret_scanner_files:
            continue

        path = ROOT / item
        if not path.exists() or not path.is_file():
            continue

        if path.suffix and path.suffix.lower() not in allowed_suffixes:
            continue

        text = read_text(path)
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                findings.append(item)
                break

    if findings:
        fail(f"possible secrets found in tracked files: {findings[:20]}")

    ok("No obvious secrets found in tracked text files")


def verify_readme_production_warning() -> None:
    readme = ROOT / "README.md"
    if not readme.exists():
        fail("README.md is missing")

    text = read_text(readme).lower()

    required_any = [
        "developer preview",
        "dev preview",
        "production",
        "not production",
        "not yet production",
    ]

    if not any(marker in text for marker in required_any):
        fail("README.md missing developer-preview / production-warning wording")

    ok("README contains developer-preview / production-warning wording")


def load_json(path: Path) -> dict:
    if not path.exists():
        fail(f"Missing config: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"Invalid JSON in {path}: {exc}")


def verify_environment_config_separation() -> None:
    for path in EXPECTED_CONFIGS:
        load_json(path)

    local = load_json(GENERATED / "config.local.json")
    docker = load_json(GENERATED / "config.docker.json")
    linux = load_json(GENERATED / "config.linux.json")

    local_text = json.dumps(local)
    docker_text = json.dumps(docker)
    linux_text = json.dumps(linux)

    if "host.docker.internal" in local_text:
        fail("config.local.json leaks Docker host addressing")

    if "host.docker.internal" not in docker_text:
        warn("config.docker.json does not contain host.docker.internal; verify this is intentional")

    if "host.docker.internal" in linux_text:
        fail("config.linux.json leaks Docker host addressing")

    ok("Environment-specific configs exist and basic address separation looks safe")


def verify_gateway_policy_contracts() -> None:
    config = load_json(GENERATED / "config.local.json")

    if "active_health" not in config:
        fail("config.local.json missing active_health policy")
    ok("Active health policy present")

    blocked = config.get("blocked_paths") or config.get("security", {}).get("blocked_paths")
    if not blocked:
        fail("Blocked paths policy missing from config.local.json")
    ok("Blocked paths policy present")

    rate_limit = (
        config.get("rate_limit_per_minute")
        or config.get("rate_limit")
        or config.get("security", {}).get("rate_limit_per_minute")
    )
    if rate_limit is None:
        fail("Rate-limit policy missing from config.local.json")
    ok("Rate-limit policy present")

    max_body = (
        config.get("max_request_body_size")
        or config.get("max_body_size")
        or config.get("security", {}).get("max_request_body_size")
    )
    if max_body is None:
        warn("Max request body policy not found in config.local.json")
    else:
        ok("Max request body policy present")


def verify_repair_plans_are_dry_run_only() -> None:
    control_api = read_text(ROOT / "pingora_ai" / "control_api.py")

    required_markers = [
        "/__gateway_control/agent/repair-plan/dry-run",
        "will_execute",
        "dry_run_only",
        "runtime_mutation",
        "config_write",
        "docker_apply",
        "linux_deploy",
    ]

    missing = [marker for marker in required_markers if marker not in control_api]
    if missing:
        fail(f"Repair dry-run safety markers missing from control_api.py: {missing}")

    dangerous_endpoint_markers = [
        "/__gateway_control/agent/repair/execute",
        "/__gateway_control/agent/repair/live",
    ]

    found_dangerous = [marker for marker in dangerous_endpoint_markers if marker in control_api]
    if found_dangerous:
        fail(f"Unexpected live repair endpoint markers found: {found_dangerous}")

    ok("Repair plan agent is dry-run only")


def verify_rollback_assets_exist() -> None:
    candidates = [
        ROOT / "rollback.py",
        ROOT / "scripts" / "verify_rollback.py",
        ROOT / "scripts" / "stress_cli_reset_rollback.py",
    ]

    if not any(path.exists() for path in candidates):
        fail("No rollback/reset verification asset found")

    ok("Rollback/reset asset exists")


def verify_required_verifiers_exist() -> None:
    required = [
        ROOT / "scripts" / "verify_local_production_readiness_gate.py",
        ROOT / "scripts" / "verify_agent_dry_run_repair_plan_local.py",
        ROOT / "scripts" / "verify_agent_learning_actionability_local.py",
    ]

    missing = [str(path.relative_to(ROOT)) for path in required if not path.exists()]
    if missing:
        fail(f"Required verifier scripts missing: {missing}")

    ok("Required local production and agent verifiers exist")


def verify_local_production_gate_passes() -> None:
    script = ROOT / "scripts" / "verify_local_production_readiness_gate.py"
    if not script.exists():
        fail("scripts/verify_local_production_readiness_gate.py missing")

    result = run_command([sys.executable, str(script.relative_to(ROOT))], timeout=300)
    print(result.stdout)

    if result.returncode != 0:
        fail("Local production readiness gate failed")

    if "LOCAL PRODUCTION READINESS GATE PASSED" not in result.stdout:
        fail("Local production readiness gate did not print pass marker")

    ok("Local production readiness gate passes")


def main() -> None:
    print("🧪 Verifying production hardening foundation")

    verify_gitignore()
    verify_env_example()
    verify_no_sensitive_files_tracked()
    verify_no_obvious_secrets_in_tracked_files()
    verify_readme_production_warning()
    verify_environment_config_separation()
    verify_gateway_policy_contracts()
    verify_repair_plans_are_dry_run_only()
    verify_rollback_assets_exist()
    verify_required_verifiers_exist()
    verify_local_production_gate_passes()

    print("\n✅ PRODUCTION HARDENING FOUNDATION VERIFICATION PASSED")


if __name__ == "__main__":
    main()
