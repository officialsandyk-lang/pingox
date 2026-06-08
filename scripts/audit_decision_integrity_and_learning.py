from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run(cmd: list[str], timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def assert_ok(name: str, ok: bool, detail: str = "") -> None:
    if not ok:
        print(f"❌ {name}")
        if detail:
            print(detail[-3000:])
        raise SystemExit(1)
    print(f"✅ {name}")


def load_config() -> dict:
    path = PROJECT_ROOT / "generated-pingora-proxy" / "config.json"
    assert_ok("generated config exists", path.exists())
    return json.loads(path.read_text(encoding="utf-8"))


def get_api_upstream_count(config: dict) -> int:
    for route in config.get("routes") or []:
        if not isinstance(route, dict):
            continue

        if route.get("path") != "/api":
            continue

        upstreams = route.get("upstreams")
        if isinstance(upstreams, list):
            return len(upstreams)

        if route.get("upstream") or route.get("backend"):
            return 1

        return 0

    return 0


def check_decision_integrity_module() -> None:
    code = r'''
from core.decision_integrity import validate_algorithm_decision, latest_lessons, latest_decisions

try:
    validate_algorithm_decision(
        route="/api",
        algorithm="round_robin",
        upstream_count=1,
        runtime="audit",
        strategy="audit",
        stage="audit_decision_integrity",
    )
except Exception as exc:
    print("BLOCKED_BY_DECISION_INTEGRITY")
    print(str(exc))
else:
    print("ERROR_NOT_BLOCKED")
    raise SystemExit(1)

lessons = latest_lessons(5)
decisions = latest_decisions(5)

print("LESSONS:", len(lessons))
print("DECISIONS:", len(decisions))

if not any(item.get("lesson_key") == "multi_algorithm_requires_multiple_upstreams" for item in lessons):
    print("ERROR_LESSON_NOT_STORED")
    raise SystemExit(1)

if not any(item.get("decision") == "reject" for item in decisions):
    print("ERROR_REJECT_DECISION_NOT_STORED")
    raise SystemExit(1)
'''
    result = run([sys.executable, "-c", code])
    output = result.stdout + "\n" + result.stderr

    assert_ok(
        "decision integrity blocks invalid algorithm and stores lesson",
        result.returncode == 0
        and "BLOCKED_BY_DECISION_INTEGRITY" in output
        and "LESSONS:" in output
        and "DECISIONS:" in output,
        output,
    )


def check_system_decision_engine() -> None:
    code = r'''
import json
from pathlib import Path
from core.system_decision_engine import apply_and_enforce_system_decisions

cfg = json.loads(Path("generated-pingora-proxy/config.json").read_text())

for route in cfg.get("routes", []):
    if route.get("path") == "/api":
        route["balancing"] = "round_robin"
        route["algorithm"] = "round_robin"

try:
    apply_and_enforce_system_decisions(
        cfg,
        prompt="set /api algorithm to round_robin CONFIRM ROUTE MUTATION",
        strict_security=False,
        allow_repair=True,
    )
except Exception as exc:
    print("BLOCKED_BY_SYSTEM_DECISION_ENGINE")
    print(str(exc))
    raise SystemExit(0)

print("ERROR_NOT_BLOCKED")
raise SystemExit(1)
'''
    result = run([sys.executable, "-c", code])
    output = result.stdout + "\n" + result.stderr

    assert_ok(
        "system decision engine blocks invalid algorithm",
        result.returncode == 0 and "BLOCKED_BY_SYSTEM_DECISION_ENGINE" in output,
        output,
    )


def check_config_update_agent() -> None:
    code = r'''
import json
from pathlib import Path
from agents.config_update_agent import apply_config_update

cfg = json.loads(Path("generated-pingora-proxy/config.json").read_text())
result = apply_config_update(cfg, "set /api algorithm to round_robin CONFIRM ROUTE MUTATION")

updated = result.get("config") or cfg
api = next((r for r in updated.get("routes", []) if r.get("path") == "/api"), {})
balancing = api.get("balancing") or api.get("algorithm")

print(json.dumps({
    "changed": result.get("changed"),
    "understood": result.get("understood"),
    "balancing": balancing,
    "summary": result.get("change_summary") or result.get("summary") or [],
}, indent=2))

if balancing == "round_robin":
    print("ERROR_AGENT_ALLOWED_INVALID_ROUND_ROBIN")
    raise SystemExit(1)

print("AGENT_SAFE")
'''
    result = run([sys.executable, "-c", code])
    output = result.stdout + "\n" + result.stderr

    assert_ok(
        "config update agent does not output invalid round_robin",
        result.returncode == 0 and "AGENT_SAFE" in output,
        output,
    )


def check_warning_downgrade_guard() -> None:
    code = r'''
from core.decision_integrity import assert_no_rejected_decision_downgraded_to_warning

warnings = [
    {
        "stage": "system_decision_engine",
        "warning": "System decision engine rejected config: Cannot set /api algorithm to round_robin: route has only 1 upstream."
    }
]

try:
    assert_no_rejected_decision_downgraded_to_warning(
        warnings=warnings,
        runtime="bare_metal_linux",
        strategy="bluegreen",
    )
except Exception as exc:
    print("WARNING_DOWNGRADE_BLOCKED")
    print(str(exc))
    raise SystemExit(0)

print("ERROR_WARNING_DOWNGRADE_ALLOWED")
raise SystemExit(1)
'''
    result = run([sys.executable, "-c", code])
    output = result.stdout + "\n" + result.stderr

    assert_ok(
        "rejected decision cannot be downgraded to warning",
        result.returncode == 0 and "WARNING_DOWNGRADE_BLOCKED" in output,
        output,
    )


def check_cli_does_not_deploy(env_flag: str, extra: list[str] | None = None) -> None:
    extra = extra or []

    cmd = [
        "pingora-ai",
        "edit",
        env_flag,
        *extra,
        "/api",
        "--algorithm",
        "round_robin",
        "--confirm-route-mutation",
    ]

    result = run(cmd)
    output = result.stdout + "\n" + result.stderr

    blocked = (
        "Cannot set /api" in output
        or "route has only 1 upstream" in output
        or "algorithm_requires_multiple_upstreams" in output
        or "System decision engine rejected config" in output
    )

    assert_ok(f"{env_flag} invalid edit is blocked", blocked, output)

    assert_ok(
        f"{env_flag} invalid edit does not start deployment",
        "Deploying to bare_metal_linux target" not in output
        and "Transactional Docker blue/green update" not in output
        and "Compiling generated-pingora-proxy" not in output,
        output,
    )


def main() -> int:
    cfg = load_config()
    upstream_count = get_api_upstream_count(cfg)

    assert_ok(
        "/api has one upstream for invalid-algorithm audit",
        upstream_count == 1,
        f"Current /api upstream_count={upstream_count}. Use a one-upstream baseline first.",
    )

    print("\n=== Decision integrity module ===")
    check_decision_integrity_module()

    print("\n=== Config update agent ===")
    check_config_update_agent()

    print("\n=== System decision engine ===")
    check_system_decision_engine()

    print("\n=== Warning downgrade guard ===")
    check_warning_downgrade_guard()

    print("\n=== CLI / Docker early block ===")
    check_cli_does_not_deploy("--docker")

    print("\n=== CLI / Local early block ===")
    check_cli_does_not_deploy("--local")

    print("\n=== CLI / Linux early block ===")
    check_cli_does_not_deploy(
        "--linux",
        [
            "--host",
            "132.145.51.57",
            "--user",
            "ubuntu",
            "--ssh-key",
            str(Path.home() / ".ssh" / "oci_pingora_key_2"),
        ],
    )

    print("\n✅ DECISION INTEGRITY + LESSON LEARNING AUDIT PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
