from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
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


def api_upstream_count(config: dict) -> int:
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
        "system decision engine blocks one-upstream round_robin",
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

print(json.dumps({
    "success": result.get("success"),
    "changed": result.get("changed"),
    "understood": result.get("understood"),
    "summary": result.get("change_summary") or result.get("summary") or [],
}, indent=2))

updated = result.get("config") or cfg
api = next((r for r in updated.get("routes", []) if r.get("path") == "/api"), {})
balancing = api.get("balancing") or api.get("algorithm")

if balancing == "round_robin":
    print("ERROR_AGENT_ALLOWED_INVALID_ROUND_ROBIN")
    raise SystemExit(1)

if "Blocked invalid algorithm edit" in json.dumps(result):
    print("BLOCKED_BY_CONFIG_UPDATE_AGENT")
    raise SystemExit(0)

# Accept unchanged single as safe if agent did not apply invalid mutation.
if balancing in {None, "", "single"}:
    print("SAFE_AGENT_DID_NOT_APPLY_INVALID_MUTATION")
    raise SystemExit(0)

print("UNKNOWN_AGENT_RESULT")
raise SystemExit(1)
'''
    result = run([sys.executable, "-c", code])
    output = result.stdout + "\n" + result.stderr

    assert_ok(
        "config update agent does not produce invalid round_robin",
        result.returncode == 0 and (
            "BLOCKED_BY_CONFIG_UPDATE_AGENT" in output
            or "SAFE_AGENT_DID_NOT_APPLY_INVALID_MUTATION" in output
        ),
        output,
    )


def check_cli_env(flag: str, extra: list[str] | None = None) -> None:
    extra = extra or []

    cmd = [
        "pingora-ai",
        "edit",
        flag,
        *extra,
        "/api",
        "--algorithm",
        "round_robin",
        "--confirm-route-mutation",
    ]

    result = run(cmd, timeout=180)
    output = result.stdout + "\n" + result.stderr

    blocked = (
        "algorithm_requires_multiple_upstreams" in output
        or "Cannot set /api algorithm to round_robin" in output
        or "Cannot set /api to round_robin" in output
        or "route has only 1 upstream" in output
        or "requires at least 2 upstreams" in output
    )

    assert_ok(f"{flag} invalid edit is blocked", blocked, output)

    assert_ok(
        f"{flag} invalid edit does not start build/deploy",
        "Compiling generated-pingora-proxy" not in output
        and "Transactional Docker blue/green update" not in output
        and "Deploying to bare_metal_linux target" not in output
        and "Project generated successfully" not in output,
        output,
    )


def main() -> int:
    config = load_config()
    count = api_upstream_count(config)

    assert_ok(
        "/api has exactly one upstream for this audit",
        count == 1,
        f"/api upstream_count={count}; run a single-upstream baseline first.",
    )

    print("\n=== Stage 1: config update agent ===")
    check_config_update_agent()

    print("\n=== Stage 2: system decision engine ===")
    check_system_decision_engine()

    print("\n=== Stage 3: CLI/deploy preflight, Docker ===")
    check_cli_env("--docker")

    print("\n=== Stage 4: CLI/deploy preflight, Local ===")
    check_cli_env("--local")

    print("\n=== Stage 5: CLI/deploy preflight, Linux ===")
    check_cli_env(
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

    print("\n✅ DECISION-MAKING INVALID ALGORITHM AUDIT PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
