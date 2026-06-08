from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from pingora_ai.api_client import plan_gateway, get_api_url

# Ensure project root is importable when running from installed console script.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.homepage_route import ensure_route_directory_homepage
from core.project_writer import write_json_config, write_route_directory_homepage

CLI_DESCRIPTION = """
AI Pingora Gateway CLI

Create, run, and update an AI gateway from plain English.

Common commands:

  pingora-ai plan
    Create a plan only. Does not run anything.

  pingora-ai apply
    Create and run a new gateway.

  pingora-ai add
    Add a new route.

  pingora-ai edit
    Change an existing route.

  pingora-ai remove
    Remove a route.

Where to run:

  --docker
    Run with Docker. Recommended.

  --local
    Run directly on this computer.

  --linux
    Run on a Linux server using SSH.

Examples:

  Run with Docker:

    pingora-ai apply --docker "create gateway with /api to 127.0.0.1:9101"

  Run with three backend servers:

    pingora-ai apply --docker "create gateway with /api random across 127.0.0.1:9101, 127.0.0.1:9102 and 127.0.0.1:9103"

  Run on this computer:

    pingora-ai apply --local "create gateway with /api to 127.0.0.1:9101"

  Run on a Linux server:

    pingora-ai apply --linux --host 1.2.3.4 --user ubuntu --ssh-key ~/.ssh/id_rsa "create gateway with /api to 127.0.0.1:9101"

  Add a route:

    pingora-ai add /coupons 127.0.0.1:9401

  Edit a route:

    pingora-ai edit /api --algorithm round_robin --confirm-route-mutation

  Remove a route:

    pingora-ai remove /coupons --confirm-route-mutation

Safety:

  add does not need confirmation.
  edit/remove/delete need --confirm-route-mutation.
  The / homepage route is protected.
"""




def _confirm_route_mutation_args(args) -> list[str]:
    if bool(getattr(args, "confirm_route_mutation", False)):
        return ["--confirm-route-mutation"]
    return []


def _append_confirm_route_mutation(cmd: list[str], args) -> list[str]:
    if bool(getattr(args, "confirm_route_mutation", False)):
        cmd.append("--confirm-route-mutation")
    return cmd


def find_project_root() -> Path | None:
    env_root = os.environ.get("PINGORA_AI_PROJECT_ROOT")

    candidates: list[Path] = []

    if env_root:
        candidates.append(Path(env_root).expanduser())

    candidates.append(Path.cwd())
    candidates.append(Path(__file__).resolve().parents[1])

    for candidate in candidates:
        candidate = candidate.resolve()

        if (candidate / "main.py").exists() and (candidate / "update.py").exists():
            return candidate

    return None


PROJECT_ROOT = find_project_root()

# Source-tree operations need project root. Pure installed-CLI commands
# such as `edge enable` must work without a source checkout.
if PROJECT_ROOT and str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def require_project_root() -> Path:
    if PROJECT_ROOT:
        return PROJECT_ROOT

    raise SystemExit(
        "Could not find project root. Run from the pingora-generator folder "
        "or set PINGORA_AI_PROJECT_ROOT. This is required for source-tree "
        "operations such as apply/update, but not for edge enable."
    )


def get_work_dir() -> Path:
    """
    Return the working directory for CLI-generated files.

    Source/developer mode:
      uses repo root.

    Installed user CLI mode:
      uses ~/.pingora-ai so plan/edge commands work without cloning the repo.
    """
    if PROJECT_ROOT:
        return PROJECT_ROOT

    work_dir = Path(os.environ.get("PINGORA_AI_HOME", "~/.pingora-ai")).expanduser()
    work_dir.mkdir(parents=True, exist_ok=True)
    return work_dir


def add_linux_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default=None, help="Linux server host/IP.")
    parser.add_argument("--user", default="ubuntu", help="SSH user.")
    parser.add_argument("--ssh-key", default=None, help="Path to SSH private key.")
    parser.add_argument("--ssh-port", type=int, default=22, help="SSH port.")
    parser.add_argument("--server-name", default="_", help=argparse.SUPPRESS)
    parser.add_argument("--public-http-port", type=int, default=80, help=argparse.SUPPRESS)
    parser.add_argument(
        "--binary-path",
        default="generated-pingora-proxy/target/release/generated-pingora-proxy-linux",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--service-user", default="ubuntu", help=argparse.SUPPRESS)
    parser.add_argument("--verify", default="full", help=argparse.SUPPRESS)



def apply_runtime_shortcuts(args: argparse.Namespace) -> None:
    """
    Normalize runtime flags once.

    --docker: docker_host + bluegreen, no systemd/native public-port flags.
    --linux: bare_metal_linux + bluegreen, no local systemd flags.
    --local: local + direct, may use --public-port/--systemd/--replace-edge.
    """
    if getattr(args, "linux", False):
        args.target = "linux"
        args.runtime = "bare_metal_linux"
        args.strategy = "bluegreen"
        args.systemd = False
        args.replace_edge = False
        return

    if getattr(args, "docker", False):
        args.target = "docker_host"
        args.runtime = "docker_host"
        args.strategy = "bluegreen"
        args.systemd = False
        args.replace_edge = False
        args.public_port = None
        return

    if getattr(args, "local", False):
        args.target = "local"
        args.runtime = "local"
        args.strategy = "direct"
        return


def enforce_requested_environment(config: dict, args: argparse.Namespace) -> dict:
    """
    Enforce the runtime/strategy explicitly requested by CLI flags.

    Hosted planner may return default docker_host/bluegreen even when the
    user requested --local. CLI flags are treated as the source of truth.

    Rule:
      hosted planner suggests
      local deterministic CLI decides
      config verification proves
    """
    if not isinstance(config, dict):
        return config

    if getattr(args, "local", False):
        runtime = "local"
        strategy = "direct"
    elif getattr(args, "docker", False):
        runtime = "docker_host"
        strategy = "bluegreen"
    elif getattr(args, "linux", False):
        runtime = "bare_metal_linux"
        strategy = "bluegreen"
    else:
        runtime = getattr(args, "runtime", None) or config.get("runtime") or "docker_host"
        strategy = getattr(args, "strategy", None) or config.get("strategy") or "bluegreen"

    config["runtime"] = runtime
    config["strategy"] = strategy
    config["runtime_mode"] = runtime
    config["deployment_strategy"] = strategy

    return config


def join_prompt(parts: list[str]) -> str:
    prompt = " ".join(parts).strip()

    if not prompt:
        raise SystemExit("Missing prompt text.")

    return prompt


def normalize_path(path: str) -> str:
    value = str(path or "").strip()

    if not value:
        raise SystemExit("Missing route path.")

    if not value.startswith("/"):
        value = "/" + value

    return value.rstrip("/") if len(value) > 1 else value


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def run_command(command: list[str], cwd: Path) -> int:
    print()
    print("Running local engine:")
    print(" ".join(command))
    print()

    process = subprocess.run(command, cwd=str(cwd))
    return process.returncode


def _load_current_gateway_config(project_root: Path) -> dict:
    candidates = [
        project_root / "generated-pingora-proxy" / "config.json",
        project_root / "active_config.json",
        project_root / "gateway_config.json",
        project_root / "data" / "api_plans" / "latest_config.json",
    ]

    for path in candidates:
        if not path.exists():
            continue

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue

        if isinstance(data, dict) and isinstance(data.get("routes"), list):
            return data

    raise SystemExit(
        "Cannot find an existing gateway config to update. "
        "Run `pingora-ai apply --linux ...` first."
    )


def _check_update_noop_or_conflict(prompt: str, args: argparse.Namespace) -> dict | None:
    from update import check_add_route_noop_or_backend_conflict

    guard_args = argparse.Namespace(**vars(args))
    guard_args.prompt = [prompt]
    return check_add_route_noop_or_backend_conflict(guard_args)


def run_bare_metal_update_prompt(
    prompt: str,
    args: argparse.Namespace,
    *,
    confirm_route_mutation: bool = False,
) -> int:
    if not getattr(args, "host", None):
        raise SystemExit("--host is required for Linux add/edit/remove.")

    if not getattr(args, "ssh_key", None):
        raise SystemExit("--ssh-key is required for Linux add/edit/remove.")

    from agents.config_update_agent import apply_config_update
    from core.environment_deployer import apply_profiles, create_backup
    from core.mutation_decision import enforce_mutation_decision
    from update import (
        assert_untouched_routes_preserved,
        preserve_untouched_routes,
        route_mutation_paths,
    )

    project_root = require_project_root()
    current = _load_current_gateway_config(project_root)

    # Linux/bare-metal early decision-integrity gate.
    # This must run before mutation decision, backup, SSH, build, or deploy.
    # --confirm-route-mutation confirms user intent; it must not bypass
    # deterministic load-balancer validity rules.
    try:
        from core.decision_integrity import validate_algorithm_decision

        route_path = getattr(args, "path", None)
        algorithm = getattr(args, "algorithm", None)

        if route_path and algorithm:
            normalized_route = normalize_path(route_path)

            upstream_count = 0
            for route in current.get("routes") or []:
                if not isinstance(route, dict):
                    continue

                if normalize_path(route.get("path") or "/") != normalized_route:
                    continue

                upstreams = route.get("upstreams")
                if isinstance(upstreams, list):
                    upstream_count = len(upstreams)
                elif route.get("upstream") or route.get("backend"):
                    upstream_count = 1
                break

            validate_algorithm_decision(
                route=normalized_route,
                algorithm=algorithm,
                upstream_count=upstream_count,
                runtime="bare_metal_linux",
                strategy="bluegreen",
                stage="cli_linux_update_preflight",
            )

    except RuntimeError as exc:
        print("")
        print("🧠 Decision integrity check")
        print("Decision: reject")
        print("Stage: cli_linux_update_preflight")
        print("Reason: algorithm_requires_multiple_upstreams")
        print("")
        print(f"⚠️ {exc}")
        print("No deployment was made.")
        return 0

    no_change = _check_update_noop_or_conflict(prompt, args)
    if no_change is not None:
        print("")
        print(no_change.get("message", "No deployment needed."))
        print("")
        print(json.dumps(no_change, indent=2))
        return 0 if no_change.get("success") else 2

    enforce_mutation_decision(
        prompt=prompt,
        config=current,
        confirmed=bool(confirm_route_mutation),
    )

    mutable_paths = route_mutation_paths(prompt)

    current_profiled = apply_profiles(
        current,
        runtime_mode="bare_metal_linux",
        strategy="bluegreen",
    )

    update_result = apply_config_update(current, prompt)
    updated = update_result.get("config") or current

    updated = preserve_untouched_routes(
        current,
        updated,
        mutable_paths,
    )

    updated.setdefault("port", current.get("port", 8080))
    updated.setdefault("security", current.get("security", {}))

    updated = apply_profiles(
        updated,
        runtime_mode="bare_metal_linux",
        strategy="bluegreen",
    )

    updated = preserve_untouched_routes(
        current_profiled,
        updated,
        mutable_paths,
    )

    assert_untouched_routes_preserved(
        current_profiled,
        updated,
        mutable_paths,
    )

    updated["runtime"] = "bare_metal_linux"
    updated["runtime_mode"] = "bare_metal_linux"
    updated["strategy"] = "bluegreen"
    updated["deployment_strategy"] = "bluegreen"

    backup = create_backup(reason="before_bare_metal_linux_update")

    if backup:
        try:
            from core.backup_registry import record_backup

            record_backup(
                backup_path=backup,
                reason="before_bare_metal_linux_update",
                runtime="bare_metal_linux",
                strategy="bluegreen",
                route_change=prompt,
                valid_before_backup=True,
                evidence={"stage": "after_decision_integrity_preflight"},
            )
        except Exception:
            pass

    print("")
    print("🛠️ AI Pingora Gateway Linux Update")
    print(f"Runtime: {updated['runtime']}")
    print(f"Strategy: {updated['strategy']}")
    print("Route changes:")
    for item in update_result.get("change_summary", []):
        print(f"- {item}")
    if backup:
        print(f"Backup: {backup}")

    return _deploy_bare_metal_from_cli(args, updated)


def looks_like_route_delete(prompt: str) -> bool:
    text = prompt.lower()

    return bool(
        re.search(r"\b(remove|delete|drop)\b\s+/[a-zA-Z0-9_\-/.]*", text)
        or re.search(r"\b(remove|delete|drop)\b\s+route\s+/[a-zA-Z0-9_\-/.]*", text)
    )


def run_update_prompt(
    prompt: str,
    runtime: str,
    strategy: str,
    verify: str,
    confirm_route_mutation: bool = False,
    cli_args: argparse.Namespace | None = None,
) -> int:
    if confirm_route_mutation and "CONFIRM ROUTE MUTATION" not in prompt:
        prompt = prompt + " CONFIRM ROUTE MUTATION"

    if runtime in {"bare_metal_linux", "linux"}:
        if cli_args is None:
            raise SystemExit("Internal error: Linux update requires CLI args.")

        return run_bare_metal_update_prompt(
            prompt,
            cli_args,
            confirm_route_mutation=confirm_route_mutation,
        )

    project_root = require_project_root()
    update_py = project_root / "update.py"

    if not update_py.exists():
        raise SystemExit(f"Cannot find update.py at: {update_py}")

    command = [
        sys.executable,
        "update.py",
        "--runtime",
        runtime,
        "--strategy",
        strategy,
        "--verify",
        verify,
    ]

    if confirm_route_mutation:
        command.append("--confirm-route-mutation")

    command.append(prompt)

    try:
        return run_command(command, project_root)

    except Exception as exc:
        error_text = str(exc)

        try:
            from core.agent_incident_commander import record_runtime_failure_lesson

            incident = record_runtime_failure_lesson(
                runtime=runtime,
                strategy=strategy,
                operation="update",
                error=error_text,
                config={},
                allow_auto_repair=True,
            )

            selected = incident.get("selected_root_cause") or {}

            print()
            print("🧠 Agent incident commander stored update failure lesson")
            print(f"Root cause: {selected.get('root_cause')}")
            print(f"Repair action: {selected.get('repair_action')}")
            print(f"Incident ID: {incident.get('incident_id')}")

        except Exception as commander_exc:
            print(f"⚠️ Agent incident commander failed to store lesson: {commander_exc}")

        raise




def ensure_planner_available_or_explain() -> None:
    """
    Check local FastAPI planner before plan/apply.

    If unavailable, raise a clean human-readable error instead of a long urllib traceback.
    """
    import urllib.request
    import urllib.error

    health_url = get_api_url() + "/health"

    try:
        with urllib.request.urlopen(health_url, timeout=3) as response:
            if int(response.status) == 200:
                return
    except Exception as exc:
        try:
            from core.agent_incident_commander import record_runtime_failure_lesson

            record_runtime_failure_lesson(
                runtime="control_plane",
                strategy="planner",
                operation="planner_health",
                error=f"FastAPI planner unavailable at {health_url}: {exc}",
                config={},
                allow_auto_repair=False,
            )
        except Exception:
            pass

        raise SystemExit(
            "\\n❌ FastAPI planner is not running.\\n\\n"
            "Reason:\\n"
            "  pingora-ai needs the planner API at http://127.0.0.1:8000, "
            "but the connection was refused.\\n\\n"
            "Fix:\\n"
            "  Open a new terminal and run:\\n\\n"
            "    cd ~/dev/pingora-generator\\n"
            "    source .venv/bin/activate\\n"
            "    uvicorn server.main:app --host 127.0.0.1 --port 8000 --reload\n\n    Or set:\n    export PINGORA_AI_API_URL=https://your-api-domain\\n\\n"
            "Then retry your pingora-ai command.\\n"
        )



def _normalise_prompt_text(prompt: str) -> str:
    return " ".join((prompt or "").strip().lower().replace("?", " ").split())


def is_onboarding_prompt(prompt: str) -> bool:
    text = _normalise_prompt_text(prompt)

    if not text:
        return True

    exact = {
        "what do you do",
        "what you do",
        "what can you do",
        "how can you help",
        "how you can help",
        "what is this",
        "what is pingora ai",
        "what is pingora-ai",
        "explain this",
        "explain yourself",
        "help",
        "help me",
        "start",
        "intro",
        "introduction",
        "hello",
        "hi",
    }

    if text in exact:
        return True

    phrases = [
        "what do you",
        "what you do",
        "how can you help",
        "how to use",
        "how does this work",
        "what is this system",
        "tell me about",
        "introduce",
    ]

    return any(p in text for p in phrases)


def looks_like_gateway_request(prompt: str) -> bool:
    """
    Keep this intentionally simple and deterministic.
    If it does not look like gateway/proxy/route/edge work,
    do not send it to hosted planner.
    """
    text = _normalise_prompt_text(prompt)

    gateway_words = {
        "gateway",
        "route",
        "routes",
        "api",
        "backend",
        "backends",
        "upstream",
        "upstreams",
        "proxy",
        "reverse",
        "load",
        "balance",
        "balancer",
        "algorithm",
        "random",
        "round",
        "robin",
        "weighted",
        "ip_hash",
        "least",
        "connections",
        "port",
        "ports",
        "nginx",
        "pingora",
        "edge",
        "public",
        "tls",
        "ssl",
        "https",
        "http",
        "block",
        "allow",
        "firewall",
        "health",
        "deploy",
        "apply",
        "create",
        "add",
        "remove",
        "delete",
        "edit",
        "change",
        "make",
        "live",
        "coupon",
        "coupons",
    }

    # Path-like or address-like prompt is probably gateway-related.
    if "/" in text or "127.0.0.1" in text or "localhost" in text:
        return True

    # Port numbers like 9101 are strong gateway signals.
    if any(part.isdigit() and 1 <= int(part) <= 65535 for part in text.split() if part.isdigit()):
        return True

    return any(word in text for word in gateway_words)


def print_out_of_context_message(prompt: str) -> None:
    print(f"""
I understand your message, but I will not create a gateway plan from it because it does not look like a gateway, route, proxy, edge, or deployment request.

You can ask me in simple language, for example:

  pingora-ai plan --local "create route /coupons to port 9101"

  pingora-ai apply --local "create gateway with /api to 127.0.0.1:9101"

  pingora-ai add --local /coupons 127.0.0.1:9101

  pingora-ai remove --local /coupons --confirm-route-mutation

  pingora-ai edge enable

For help, run:

  pingora-ai plan "what do you do"

Your input was:
  {prompt}
""".strip())


def print_onboarding() -> None:
    print("""
AI Pingora Gateway helps you create and run API gateways from simple language.

You can say things like:

  pingora-ai edge enable

  pingora-ai plan --local "create route /coupons to port 9101"

  pingora-ai apply --local "create gateway with /api to 127.0.0.1:9101"

  pingora-ai add --local /coupons 127.0.0.1:9101

  pingora-ai edit --local /api --algorithm random --confirm-route-mutation

  pingora-ai remove --local /coupons --confirm-route-mutation

What it checks:
  - public edge readiness
  - Nginx and firewall rules
  - route and upstream safety
  - load-balancing algorithm rules
  - blocked sensitive paths
  - runtime/environment mismatch
  - deployment health

Plain language is okay:
  "create route for coupons on port 9101"
  "remove existing coupons route and make it live"
  "change api algorithm to random"

Safety rule:
  destructive changes such as edit/remove need explicit confirmation.
""".strip())

def force_runtime_flags_into_config(config: dict, args, prompt: str = "") -> dict:
    """
    Final CLI runtime boundary.

    Hosted planner output must never override explicit CLI runtime flags.
    Cross-runtime flags are stripped here.
    """
    if not isinstance(config, dict):
        return config

    apply_runtime_shortcuts(args)

    runtime = getattr(args, "runtime", None) or config.get("runtime") or "docker_host"

    for key in (
        "_systemd",
        "systemd",
        "_replace_edge",
        "replace_edge",
        "service_user",
    ):
        config.pop(key, None)

    if runtime == "docker_host":
        config["runtime"] = "docker_host"
        config["runtime_mode"] = "docker_host"
        config["strategy"] = "bluegreen"
        config["deployment_strategy"] = "bluegreen"
        config.pop("public_port", None)
        config.pop("listen_port", None)
        config["live_url"] = "https://127.0.0.1:8443/"
        return config

    if runtime in {"bare_metal_linux", "linux"}:
        config["runtime"] = "bare_metal_linux"
        config["runtime_mode"] = "bare_metal_linux"
        config["strategy"] = "bluegreen"
        config["deployment_strategy"] = "bluegreen"

        requested_port = getattr(args, "public_port", None)
        if requested_port:
            config["public_port"] = int(requested_port)
            config["listen_port"] = int(requested_port)

        return config

    config["runtime"] = "local"
    config["runtime_mode"] = "local"
    config["strategy"] = "direct"
    config["deployment_strategy"] = "direct"

    requested_port = getattr(args, "public_port", None)
    if requested_port is None and "extract_requested_public_port" in globals():
        requested_port = extract_requested_public_port(prompt)

    port = int(requested_port or config.get("port") or config.get("public_port") or config.get("listen_port") or 8090)

    config["port"] = port
    config["public_port"] = port
    config["listen_port"] = port
    config["live_url"] = f"http://127.0.0.1:{port}/"

    if bool(getattr(args, "systemd", False)):
        config["_systemd"] = True
        config["systemd"] = True

    if bool(getattr(args, "replace_edge", False)):
        config["_replace_edge"] = True
        config["replace_edge"] = True

    return config


def repair_planner_config_from_prompt(config: dict, args: argparse.Namespace, prompt: str) -> dict:
    """
    Apply deterministic local prompt repairs after the planner API responds.

    The hosted/API planner can return a syntactically valid but semantically
    incomplete config, for example random /api with a single upstream even when
    the prompt listed three backends. The CLI keeps runtime flags authoritative
    and repairs prompt-derived route intent before decision guards or deploy.
    """
    if not isinstance(config, dict):
        return config

    from agents.config_repair_agent import repair_config
    from orchestration.graph import lock_prompt_route_intent

    repaired = repair_config(config, prompt=prompt)
    repaired = lock_prompt_route_intent(repaired, prompt)
    repaired = enforce_requested_environment(repaired, args)
    repaired = ensure_route_directory_homepage(repaired)
    return repaired


def cmd_plan(args: argparse.Namespace) -> int:
    apply_runtime_shortcuts(args)
    prompt = join_prompt(args.prompt)

    if is_onboarding_prompt(prompt):
        print_onboarding()
        return 0

    if not looks_like_gateway_request(prompt):
        print_out_of_context_message(prompt)
        return 2

    ensure_planner_available_or_explain()

    response = plan_gateway(
        prompt=prompt,
        runtime=args.runtime,
        strategy=args.strategy,
        verify=args.verify,
    )

    if response.get("ok") and isinstance(response.get("config"), dict):
        from core.system_decision_engine import apply_lightweight_decision_gate
        from core.plan_state_guard import load_existing_config, apply_plan_state_guard

        work_dir = get_work_dir()
        existing_config = load_existing_config(work_dir)

        response["config"] = repair_planner_config_from_prompt(response["config"], args, prompt)
        response["config"] = apply_lightweight_decision_gate(
            response["config"],
            args=args,
            operation="plan",
            production_mode=False,
        )

        state_guard = apply_plan_state_guard(
            prompt=prompt,
            new_config=response["config"],
            existing_config=existing_config,
        )

        if state_guard.get("halt"):
            print()
            print("⚠️ Plan halted by local decision guard")
            print(state_guard.get("message"))
            print()
            print(json.dumps({
                "ok": False,
                "halted": True,
                "reason": state_guard.get("message"),
                "decision": state_guard.get("decision"),
            }, indent=2))
            return 2

        response["config"] = state_guard.get("config") or response["config"]
        if state_guard.get("warnings"):
            response.setdefault("warnings", []).extend(state_guard["warnings"])

    work_dir = get_work_dir()
    output_path = work_dir / "data" / "api_plans" / "latest_plan.json"
    config_path = work_dir / "data" / "api_plans" / "latest_config.json"

    save_json(output_path, response)

    if response.get("ok") and isinstance(response.get("config"), dict):
        save_json(config_path, response["config"])

    print(json.dumps(response, indent=2))
    print()
    print(f"Saved plan to: {output_path}")

    return 0

def _deploy_bare_metal_from_cli(args: argparse.Namespace, config: dict) -> int:
    if not getattr(args, "host", None):
        print("❌ --host is required when using --target bare_metal_linux")
        return 1

    from core.environment_deployer import deploy_bare_metal_with_shared_pipeline

    config = ensure_route_directory_homepage(config)

    public_port = int(getattr(args, "public_http_port", 80) or 80)
    host = str(getattr(args, "host", "") or "").strip()

    if host:
        if public_port == 80:
            config["live_url"] = f"http://{host}/"
        else:
            config["live_url"] = f"http://{host}:{public_port}/"

    config["runtime"] = "bare_metal_linux"
    config["strategy"] = "bluegreen"

    project_root = require_project_root()
    write_json_config(config, project_root / "generated-pingora-proxy")
    write_route_directory_homepage(config, project_root / "generated-pingora-proxy")

    print()
    print("🚀 Deploying to bare_metal_linux target")
    print(f"Host: {args.host}")
    print(f"User: {args.user}")
    print(f"SSH port: {args.ssh_port}")
    print(f"Public HTTP port: {args.public_http_port}")
    print(f"Binary path: {args.binary_path}")
    print()

    result = deploy_bare_metal_with_shared_pipeline(
        config,
        host=args.host,
        user=args.user,
        ssh_key=args.ssh_key,
        ssh_port=args.ssh_port,
        server_name=args.server_name,
        public_http_port=args.public_http_port,
        binary_path=args.binary_path,
        service_user=args.service_user,
        verify=args.verify,
        verify_path="/api/",
        verify_timeout_seconds=60,
    )

    print(json.dumps(result, indent=2))

    if not result.get("success"):
        print("❌ bare_metal_linux deployment failed")
        if result.get("error"):
            print(result["error"])
        return 1

    print("✅ bare_metal_linux deployment passed")
    print(f"Live URL: {result.get('live_url')}")
    print(f"Active color: {result.get('active_color')}")
    print(f"Active port: {result.get('active_port')}")
    return 0

def cmd_control_api(args: argparse.Namespace) -> int:
    import os
    import subprocess
    import sys

    token = getattr(args, "token", None) or os.getenv("PINGORA_AI_CONTROL_TOKEN") or "dev-local-control-token"
    os.environ["PINGORA_AI_CONTROL_TOKEN"] = token

    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "pingora_ai.control_api:app",
        "--host",
        str(getattr(args, "host", "127.0.0.1")),
        "--port",
        str(getattr(args, "port", 8787)),
    ]

    if getattr(args, "reload", False):
        command.append("--reload")

    print("Starting Pingora AI Control API")
    print(f"URL: http://{args.host}:{args.port}")
    print("Token header: X-Pingora-Control-Token")
    print("Mode: read-only phase 1")
    print("")
    return subprocess.call(command)



def cmd_parity(args: argparse.Namespace) -> int:
    from pingora_ai.parity_ops import run_parity_report

    return run_parity_report()



def cmd_health(args: argparse.Namespace) -> int:
    from pingora_ai.gateway_ops import health_policy

    if args.health_action == "policy":
        return health_policy(path=getattr(args, "path", None))

    print("Unknown health action.")
    return 2


def cmd_upstream(args: argparse.Namespace) -> int:
    if args.upstream_action == "list":
        from pingora_ai.parity_ops import run_upstream_list
        return run_upstream_list()

    if args.upstream_action in {"drain", "disable", "enable"}:
        from pingora_ai.gateway_ops import upstream_state_change

        return upstream_state_change(
            action=args.upstream_action,
            path=args.path,
            backend=args.backend,
            confirm=bool(getattr(args, "confirm_route_mutation", False)),
            dry_run=bool(getattr(args, "dry_run", False)),
        )

    print("Unknown upstream action.")
    return 2


def cmd_backup(args: argparse.Namespace) -> int:
    if args.backup_action == "list":
        from pingora_ai.parity_ops import run_backup_list
        return run_backup_list(limit=int(getattr(args, "limit", 20) or 20))

    if args.backup_action == "restore":
        from pingora_ai.gateway_ops import backup_restore
        return backup_restore(
            backup_name=args.backup_name,
            confirm=bool(getattr(args, "confirm", False)),
        )

    print("Unknown backup action.")
    return 2




def _start_local_runtime() -> int:
    """
    Start generated Pingora runtime locally on 8080 with local upstream addresses.
    Local only. Does not touch Docker or Linux.
    """
    import os
    import subprocess
    import sys
    import time
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    project = root / "generated-pingora-proxy"
    main_rs = project / "src" / "main.rs"

    from core.env_config_resolver import sync_env_configs, env_config_path
    sync_env_configs(project / "config.json", project, environments=["local", "docker", "linux"])
    config_path = env_config_path("local", project)

    if not main_rs.exists():
        print(f"❌ Missing generated runtime: {main_rs}")
        return 1

    print("🛑 Stopping any existing local runtime on 8080...")
    subprocess.run(
        "lsof -tiTCP:8080 -sTCP:LISTEN | xargs kill -9 2>/dev/null || true",
        shell=True,
        cwd=str(root),
    )

    print("🔁 Setting generated runtime upstreams to local address mode...")
    rust_text = main_rs.read_text(encoding="utf-8")

    backup = main_rs.with_suffix(".rs.bak-before-start-local-address-mode")
    if not backup.exists():
        backup.write_text(rust_text, encoding="utf-8")

    rust_text = rust_text.replace("host.docker.internal:", "127.0.0.1:")
    main_rs.write_text(rust_text, encoding="utf-8")

    print("🧪 Checking generated Rust runtime...")
    check = subprocess.run(
        ["cargo", "check", "--manifest-path", str(project / "Cargo.toml")],
        cwd=str(root),
        text=True,
    )

    if check.returncode != 0:
        print("❌ Local runtime cargo check failed.")
        return check.returncode

    demo_script = root / "scripts" / "demo_backends.py"
    if demo_script.exists() and config_path.exists():
        print("🚀 Starting local demo backends...")
        subprocess.run(
            [sys.executable, str(demo_script), "start", str(config_path)],
            cwd=str(root),
            text=True,
        )

    env = os.environ.copy()
    env["PINGORA_AI_LISTEN_PORT"] = "8080"

    log_dir = project / "tmp" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "local-pingora-8080.log"

    print("🚀 Starting local Pingora runtime on 8080...")
    with log_path.open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            ["cargo", "run"],
            cwd=str(project),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    time.sleep(3)

    status = subprocess.run(
        ["bash", "-lc", "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/ || true"],
        cwd=str(root),
        text=True,
        capture_output=True,
    )

    code = (status.stdout or "").strip()

    try:
        import json
        state_path = root / "data" / "active_runtime_context.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "runtime_context": "local",
                    "address_mode": "local",
                    "runtime": "local",
                    "strategy": "direct",
                    "base_url": "http://127.0.0.1:8080",
                    "active_config_path": str(config_path),
                    "source": "pingora-ai start --local",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"✅ Active runtime context written: {state_path}")
    except Exception as exc:
        print(f"⚠️ Could not write active runtime context: {exc}")

    print("")
    print("✅ Local Pingora runtime start requested")
    print("URL: http://127.0.0.1:8080/")
    print("Dashboard: http://127.0.0.1:8080/?runtime_context=local&address_mode=local")
    print(f"PID: {process.pid}")
    print(f"Log: {log_path}")
    print(f"HTTP status: {code}")

    return 0 if code == "200" else 1

def cmd_start(args: argparse.Namespace) -> int:
    apply_runtime_shortcuts(args)

    if getattr(args, "docker", False):
        from pingora_ai.runtime_control import start_docker
        return start_docker()

    if getattr(args, "local", False):
        return _start_local_runtime()

    print("Choose a runtime: pingora-ai start --local or pingora-ai start --docker")
    return 2


def cmd_restart(args: argparse.Namespace) -> int:
    apply_runtime_shortcuts(args)

    if getattr(args, "docker", False):
        from pingora_ai.runtime_control import restart_docker
        return restart_docker()

    print("For now, restart is implemented for --docker only.")
    print("Use: pingora-ai restart --docker")
    return 2


def cmd_reload(args: argparse.Namespace) -> int:
    apply_runtime_shortcuts(args)

    if getattr(args, "docker", False):
        from pingora_ai.runtime_control import reload_docker
        return reload_docker()

    print("For now, reload is implemented for --docker only.")
    print("Use: pingora-ai reload --docker")
    return 2



def cmd_stop(args: argparse.Namespace) -> int:
    apply_runtime_shortcuts(args)

    from pingora_ai.runtime_control import stop_docker, stop_linux, stop_local

    if getattr(args, "local", False):
        return stop_local()

    if getattr(args, "docker", False):
        return stop_docker()

    if getattr(args, "linux", False):
        return stop_linux(args)

    print("Please choose one environment: --local, --docker, or --linux.")
    return 2


def cmd_status(args: argparse.Namespace) -> int:
    apply_runtime_shortcuts(args)

    from pingora_ai.runtime_control import status_docker, status_linux, status_local

    if getattr(args, "local", False):
        return status_local()

    if getattr(args, "docker", False):
        return status_docker()

    if getattr(args, "linux", False):
        return status_linux(args)

    print("Please choose one environment: --local, --docker, or --linux.")
    return 2



def cmd_test(args: argparse.Namespace) -> int:
    from pingora_ai.test_runner import run_test_group

    return run_test_group(
        args.group,
        with_live_docker=bool(getattr(args, "with_live_docker", False)),
        with_live_linux=bool(getattr(args, "with_live_linux", False)),
    )


def cmd_doctor(args: argparse.Namespace) -> int:
    from pingora_ai.test_runner import run_doctor

    return run_doctor()



def cmd_apply(args: argparse.Namespace) -> int:
    ensure_planner_available_or_explain()
    prompt = join_prompt(args.prompt)

    # Friendly shortcuts for non-technical users.
    if getattr(args, "docker", False):
        args.target = "docker_host"
        args.runtime = "docker_host"
        args.strategy = "bluegreen"

    if getattr(args, "local", False):
        args.target = "local"
        args.runtime = "local"
        args.strategy = "direct"

    if getattr(args, "linux", False):
        args.target = "linux"
        args.runtime = "bare_metal_linux"
        args.strategy = "bluegreen"

    print("Planning gateway through FastAPI...")

    response = plan_gateway(
        prompt=prompt,
        runtime=args.runtime,
        strategy=args.strategy,
        verify=args.verify,
    )

    if not response.get("ok"):
        print(json.dumps(response, indent=2))
        raise SystemExit("API planning failed.")

    project_root = require_project_root()
    plan_path = project_root / "data" / "api_plans" / "latest_plan.json"
    config_path = project_root / "data" / "api_plans" / "latest_config.json"

    response["config"] = repair_planner_config_from_prompt(response["config"], args, prompt)

    save_json(plan_path, response)
    save_json(config_path, response["config"])

    print("API plan generated successfully.")
    print(f"Saved plan to: {plan_path}")
    print(f"Saved config to: {config_path}")

    if getattr(args, "target", None) in {"linux", "bare_metal_linux"}:
        return _deploy_bare_metal_from_cli(args, response["config"])

    main_py = project_root / "main.py"

    if not main_py.exists():
        raise SystemExit(f"Cannot find main.py at: {main_py}")

    command = [
        sys.executable,
        "main.py",
        "--runtime",
        args.runtime,
        "--strategy",
        args.strategy,
        "--verify",
        args.verify,
        "--config-file",
        str(config_path),
        "__config_file_deploy__",
    ]

    return run_command(command, project_root)


def cmd_update(args: argparse.Namespace) -> int:
    apply_runtime_shortcuts(args)
    prompt = join_prompt(args.prompt)

    return run_update_prompt(
        prompt=prompt,
        runtime=args.runtime,
        strategy=args.strategy,
        verify=args.verify,
        confirm_route_mutation=args.confirm_route_mutation,
        cli_args=args,
    )


def cmd_add(args: argparse.Namespace) -> int:
    apply_runtime_shortcuts(args)
    path = normalize_path(args.path)
    backend = str(args.backend).strip()

    if not backend:
        raise SystemExit("Missing backend.")

    prompt = f"add {path} to backend {backend}"

    return run_update_prompt(
        prompt=prompt,
        runtime=args.runtime,
        strategy=args.strategy,
        verify=args.verify,
        confirm_route_mutation=False,
        cli_args=args,
    )


def cmd_edit(args: argparse.Namespace) -> int:
    apply_runtime_shortcuts(args)
    path = normalize_path(args.path)
    algorithm = args.algorithm.strip()

    valid_algorithms = {
        "random",
        "round_robin",
        "weighted_round_robin",
        "ip_hash",
        "least_connections",
        "single",
    }

    if algorithm not in valid_algorithms:
        raise SystemExit(
            f"Invalid algorithm: {algorithm}. "
            f"Use one of: {', '.join(sorted(valid_algorithms))}"
        )

    prompt = f"set {path} algorithm to {algorithm}"

    return run_update_prompt(
        prompt=prompt,
        runtime=args.runtime,
        strategy=args.strategy,
        verify=args.verify,
        confirm_route_mutation=args.confirm_route_mutation,
        cli_args=args,
    )


def cmd_delete(args: argparse.Namespace) -> int:
    apply_runtime_shortcuts(args)
    path = normalize_path(args.path)
    prompt = f"remove {path}"

    return run_update_prompt(
        prompt=prompt,
        runtime=args.runtime,
        strategy=args.strategy,
        verify=args.verify,
        confirm_route_mutation=args.confirm_route_mutation,
        cli_args=args,
    )


def add_runtime_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--runtime",
        default="docker_host",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--strategy",
        default="bluegreen",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--verify",
        default="full",
        help=argparse.SUPPRESS,
    )

    parser.add_argument(
        "--docker",
        action="store_true",
        help="Run with Docker blue/green.",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Run directly on this computer.",
    )
    parser.add_argument(
        "--linux",
        action="store_true",
        help="Run on a Linux server using SSH.",
    )

    parser.add_argument("--host", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--user", default="ubuntu", help=argparse.SUPPRESS)
    parser.add_argument("--ssh-key", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--ssh-port", type=int, default=22, help=argparse.SUPPRESS)
    parser.add_argument("--server-name", default="_", help=argparse.SUPPRESS)
    parser.add_argument("--public-http-port", type=int, default=80, help=argparse.SUPPRESS)
    parser.add_argument(
        "--binary-path",
        default="generated-pingora-proxy/target/release/generated-pingora-proxy-linux",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--service-user", default="ubuntu", help=argparse.SUPPRESS)



def cmd_edge(args: argparse.Namespace) -> int:
    action = getattr(args, "edge_action", None)

    if action == "enable":
        from core.public_edge import enable_public_edge

        result = enable_public_edge()
        print(json.dumps(result, indent=2))

        if result.get("success"):
            print()
            print("✅ Public edge enabled locally.")
            print("If browser access still fails, open cloud firewall/Security List ports 80 and 443.")
            return 0

        print()
        print("❌ Public edge enable failed. See checks above.")
        return 1

    raise SystemExit("Unknown edge action.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pingora-ai",
        description=CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subcommands = parser.add_subparsers(dest="command", required=True)

    edge_parser = subcommands.add_parser(
        "edge",
        help="Manage public edge setup such as HTTP/HTTPS ports and Nginx bootstrap.",
    )
    edge_subcommands = edge_parser.add_subparsers(dest="edge_action", required=True)

    edge_enable = edge_subcommands.add_parser(
        "enable",
        help="Enable a public HTTP/HTTPS edge on this Linux machine.",
    )
    edge_enable.set_defaults(func=cmd_edge)

    control_api_parser = subcommands.add_parser(
        "control-api",
        help="Run authenticated local dashboard control API.",
        description="Run the authenticated local control API for dashboard operations. Phase 1 is read-only.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    control_api_parser.add_argument("--host", default="127.0.0.1")
    control_api_parser.add_argument("--port", type=int, default=8787)
    control_api_parser.add_argument("--token", default=None)
    control_api_parser.add_argument("--reload", action="store_true")
    control_api_parser.set_defaults(func=cmd_control_api)

    parity_parser = subcommands.add_parser(
        "parity",
        help="Show Production Gateway Parity report.",
        description=(
            "Read-only report showing how close the current gateway is to the "
            "Production Gateway Parity target. This does not deploy or mutate config."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parity_parser.set_defaults(func=cmd_parity)

    health_parser = subcommands.add_parser(
        "health",
        help="Inspect or manage health-check policy.",
        description="Health-check policy commands. Safe read-only policy view first.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    health_subcommands = health_parser.add_subparsers(dest="health_action", required=True)
    health_policy_parser = health_subcommands.add_parser(
        "policy",
        help="Show health-check policy.",
    )
    health_policy_parser.add_argument("path", nargs="?", default=None)
    health_policy_parser.set_defaults(func=cmd_health)

    upstream_parser = subcommands.add_parser(
        "upstream",
        help="Inspect upstreams.",
        description="Read-only upstream inspection commands.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    upstream_subcommands = upstream_parser.add_subparsers(dest="upstream_action", required=True)
    upstream_list_parser = upstream_subcommands.add_parser(
        "list",
        help="List routes and upstreams.",
    )
    upstream_list_parser.set_defaults(func=cmd_upstream)

    for action_name in ["drain", "disable", "enable"]:
        action_parser = upstream_subcommands.add_parser(
            action_name,
            help=f"{action_name} an upstream backend.",
        )
        action_parser.add_argument("path", help="Route path, for example /api")
        action_parser.add_argument("backend", help="Backend address, for example 127.0.0.1:9101")
        action_parser.add_argument("--dry-run", action="store_true")
        action_parser.add_argument("--confirm-route-mutation", action="store_true")
        action_parser.set_defaults(func=cmd_upstream)

    backup_parser = subcommands.add_parser(
        "backup",
        help="Inspect backups.",
        description="Read-only backup inspection commands.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    backup_subcommands = backup_parser.add_subparsers(dest="backup_action", required=True)
    backup_list_parser = backup_subcommands.add_parser(
        "list",
        help="List recent backups.",
    )
    backup_list_parser.add_argument("--limit", type=int, default=20)
    backup_list_parser.set_defaults(func=cmd_backup)

    backup_restore_parser = backup_subcommands.add_parser(
        "restore",
        help="Restore a backup into generated project files.",
    )
    backup_restore_parser.add_argument("backup_name")
    backup_restore_parser.add_argument("--confirm", action="store_true")
    backup_restore_parser.set_defaults(func=cmd_backup)

    start_parser = subcommands.add_parser(
        "start",
        help="Start an existing gateway runtime.",
        description="Start an existing gateway runtime. Currently implemented for --docker.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_runtime_flags(start_parser)
    start_parser.set_defaults(func=cmd_start)

    restart_parser = subcommands.add_parser(
        "restart",
        help="Restart a running gateway runtime.",
        description="Restart a running gateway runtime. Currently implemented for --docker.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_runtime_flags(restart_parser)
    restart_parser.set_defaults(func=cmd_restart)

    reload_parser = subcommands.add_parser(
        "reload",
        help="Reload a running gateway runtime.",
        description="Reload a gateway runtime. Currently implemented as safe Docker restart.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_runtime_flags(reload_parser)
    reload_parser.set_defaults(func=cmd_reload)

    stop_parser = subcommands.add_parser(
        "stop",
        help="Stop a running gateway runtime.",
        description=(
            "Stop a running gateway. You must choose --local, --docker, or --linux. "
            "This stops runtime processes/containers only. It does not delete backups or configs."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_runtime_flags(stop_parser)
    stop_parser.set_defaults(func=cmd_stop)

    status_parser = subcommands.add_parser(
        "status",
        help="Show gateway runtime status.",
        description=(
            "Show status for a gateway runtime. You must choose --local, --docker, or --linux."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_runtime_flags(status_parser)
    status_parser.set_defaults(func=cmd_status)

    doctor_parser = subcommands.add_parser(
        "doctor",
        help="Check local pingora-ai setup health.",
        description=(
            "Check local pingora-ai setup health without deploying anything."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    doctor_parser.set_defaults(func=cmd_doctor)

    test_parser = subcommands.add_parser(
        "test",
        help="Run safe test groups.",
        description=(
            "Run pingora-ai safety and functionality tests. "
            "Live Docker/Linux tests require explicit opt-in flags."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    test_parser.add_argument(
        "group",
        choices=["decision", "edge", "linux", "docker", "linux-live", "all"],
        nargs="?",
        default="all",
        help="Test group to run.",
    )
    test_parser.add_argument(
        "--with-live-docker",
        action="store_true",
        help="Allow tests that may touch live Docker.",
    )
    test_parser.add_argument(
        "--with-live-linux",
        action="store_true",
        help="Allow tests that may touch a live Linux server.",
    )
    test_parser.set_defaults(func=cmd_test)

    plan_parser = subcommands.add_parser(
        "plan",
        help="Generate config/plan only. Does not deploy.",
        description=(
            "Generate a safe gateway config/plan from natural language using "
            "the FastAPI AI planning API. This command does not run Docker "
            "and does not deploy."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_runtime_flags(plan_parser)
    plan_parser.add_argument("prompt", nargs=argparse.REMAINDER)
    plan_parser.description = 'Create a gateway plan from plain English. This does not run or deploy anything.'
    plan_parser.epilog = 'Plan examples:\n\n  Create a simple plan:\n\n    pingora-ai plan "create gateway with /api to 127.0.0.1:9101"\n\n  Create a plan with 3 backend servers:\n\n    pingora-ai plan "create gateway with /api random across 127.0.0.1:9101, 127.0.0.1:9102 and 127.0.0.1:9103"\n\n  Create a plan with multiple routes:\n\n    pingora-ai plan "create gateway with /api to 127.0.0.1:9101 and /users to 127.0.0.1:9201"\n\nWhat plan does:\n\n  - reads your plain English request\n  - creates a safe gateway config\n  - saves the plan\n  - does not deploy anything\n'
    plan_parser.formatter_class = argparse.RawDescriptionHelpFormatter

    plan_parser.set_defaults(func=cmd_plan)

    apply_parser = subcommands.add_parser(
        "apply",
        help="Create and deploy a new gateway.",
        description=(
            "Create and run an AI gateway from plain English."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_runtime_flags(apply_parser)

    apply_parser.add_argument("prompt", nargs=argparse.REMAINDER)

    

    apply_parser.add_argument(
        "--target",
        choices=["local", "docker", "docker_host", "linux", "bare_metal_linux"],
        default="docker_host",
        help=argparse.SUPPRESS,
    )

    apply_parser.description = 'Create and run an AI gateway from plain English.'
    apply_parser.epilog = 'Apply examples:\n\n  Run with Docker, recommended:\n\n    pingora-ai apply --docker "create gateway with /api to 127.0.0.1:9101"\n\n  Run with Docker and 3 backend servers:\n\n    pingora-ai apply --docker "create gateway with /api random across 127.0.0.1:9101, 127.0.0.1:9102 and 127.0.0.1:9103"\n\n  Run directly on this computer:\n\n    pingora-ai apply --local "create gateway with /api to 127.0.0.1:9101"\n\n  Run on a Linux server:\n\n    pingora-ai apply --linux --host 1.2.3.4 --user ubuntu --ssh-key ~/.ssh/id_rsa "create gateway with /api to 127.0.0.1:9101"\n\nWhat apply does:\n\n  - creates a gateway config from your plain English request\n  - deploys it to Docker, this computer, or a Linux server\n  - checks that the gateway works\n'
    apply_parser.formatter_class = argparse.RawDescriptionHelpFormatter


    apply_parser.add_argument(
        "--public-port",
        "--port",
        dest="public_port",
        type=int,
        default=None,
        help="Public/listen port for local native runtime, for example 80 or 8090.",
    )
    apply_parser.add_argument(
        "--replace-edge",
        action="store_true",
        help="Allow local native runtime to replace Nginx/public edge when using --local.",
    )
    apply_parser.add_argument(
        "--systemd",
        action="store_true",
        help="Run local native Pingora runtime through systemd, useful for privileged port 80.",
    )
    apply_parser.set_defaults(func=cmd_apply)

    update_parser = subcommands.add_parser(
        "update",
        help="Natural-language update: add, edit, or remove.",
        description=(
            "General natural-language update command. It can add, edit, or "
            "remove routes based on the prompt. It wraps the existing stable "
            "update.py engine."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_runtime_flags(update_parser)
    update_parser.add_argument(
        "--confirm-route-mutation",
        action="store_true",
        help="Confirm dangerous route mutation such as delete/remove.",
    )
    update_parser.add_argument("prompt", nargs=argparse.REMAINDER)
    update_parser.description = 'Update the running gateway using plain English.'
    update_parser.epilog = 'Update examples:\n\n  Add a route using plain English:\n\n    pingora-ai update "add /coupons to backend 127.0.0.1:9401"\n\n  Change a route algorithm:\n\n    pingora-ai update --confirm-route-mutation "set /api algorithm to round_robin"\n\n  Remove a route:\n\n    pingora-ai update --confirm-route-mutation "remove /coupons"\n\nSafety:\n\n  - adding a route does not need confirmation\n  - editing or removing a route needs --confirm-route-mutation\n  - the / homepage route is protected\n'
    update_parser.formatter_class = argparse.RawDescriptionHelpFormatter

    update_parser.set_defaults(func=cmd_update)

    add_parser = subcommands.add_parser(
        "add",
        help="Add one route shortcut.",
        description=(
            "Shortcut for adding one route. This deploys the updated gateway.\n\n"
            "Example:\n"
            "  pingora-ai add /coupons 127.0.0.1:9401"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_runtime_flags(add_parser)
    add_parser.add_argument("path", help="Route path, for example /coupons")
    add_parser.add_argument("backend", help="Backend address, for example 127.0.0.1:9401")
    add_parser.description = 'Add a new route to the running gateway.'
    add_parser.epilog = 'Add examples:\n\n  Add /coupons:\n\n    pingora-ai add /coupons 127.0.0.1:9401\n\n  Add /search:\n\n    pingora-ai add /search 127.0.0.1:9501\n\n  Add /billing:\n\n    pingora-ai add /billing 127.0.0.1:9601\n\nWhat add does:\n\n  - creates a new route\n  - keeps existing routes unchanged\n  - deploys the updated gateway\n  - checks that the new route works\n\nSafety:\n\n  add does not need --confirm-route-mutation because it does not remove or change an existing route.\n'
    add_parser.formatter_class = argparse.RawDescriptionHelpFormatter

    add_parser.set_defaults(func=cmd_add)

    edit_parser = subcommands.add_parser(
        "edit",
        help="Edit route algorithm shortcut.",
        description=(
            "Shortcut for editing a route algorithm. This deploys the updated gateway.\n\n"
            "Valid algorithms:\n"
            "  random\n"
            "  round_robin\n"
            "  weighted_round_robin\n"
            "  ip_hash\n"
            "  least_connections\n"
            "  single\n\n"
            "Example:\n"
            "  pingora-ai edit /api --algorithm round_robin"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_runtime_flags(edit_parser)
    edit_parser.add_argument("path", help="Route path, for example /api")
    edit_parser.add_argument("--algorithm", required=True, help="New route algorithm")
    edit_parser.add_argument("--confirm-route-mutation", action="store_true")
    edit_parser.description = 'Change an existing route.'
    edit_parser.epilog = 'Edit examples:\n\n  Change /api to round robin:\n\n    pingora-ai edit /api --algorithm round_robin --confirm-route-mutation\n\n  Change /api back to random:\n\n    pingora-ai edit /api --algorithm random --confirm-route-mutation\n\n  Change /users to least connections:\n\n    pingora-ai edit /users --algorithm least_connections --confirm-route-mutation\n\nSupported algorithms:\n\n  random\n  round_robin\n  weighted_round_robin\n  ip_hash\n  least_connections\n\nSafety:\n\n  edit changes a live route, so --confirm-route-mutation is required.\n'
    edit_parser.formatter_class = argparse.RawDescriptionHelpFormatter

    edit_parser.set_defaults(func=cmd_edit)

    delete_parser = subcommands.add_parser(
        "delete",
        help="Delete a route from config and live gateway.",
        description=(
            "Deletes a route from the active gateway config and live gateway.\n"
            "This is not hide/disable. It removes the route.\n\n"
            "Example:\n"
            "  pingora-ai delete /coupons"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_runtime_flags(delete_parser)
    delete_parser.add_argument("path", help="Route path to delete, for example /coupons")
    delete_parser.add_argument("--confirm-route-mutation", action="store_true")
    delete_parser.description = 'Delete a route from the gateway. Same as remove.'
    delete_parser.epilog = 'Delete examples:\n\n  Delete /coupons:\n\n    pingora-ai delete /coupons --confirm-route-mutation\n\n  Delete /search:\n\n    pingora-ai delete /search --confirm-route-mutation\n\nWhat delete does:\n\n  - same as remove\n  - removes the route from config and homepage\n  - deploys the updated gateway\n\nSafety:\n\n  delete removes a live route, so --confirm-route-mutation is required.\n  The / homepage route is protected.\n'
    delete_parser.formatter_class = argparse.RawDescriptionHelpFormatter

    delete_parser.set_defaults(func=cmd_delete)

    remove_parser = subcommands.add_parser(
        "remove",
        help="Alias for delete. Same behaviour.",
        description=(
            "Alias for delete. Same behaviour as delete.\n"
            "This is not hide/disable. It removes the route from the active "
            "gateway config and live gateway.\n\n"
            "Example:\n"
            "  pingora-ai remove /coupons"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_runtime_flags(remove_parser)
    remove_parser.add_argument("path", help="Route path to remove, for example /coupons")
    remove_parser.add_argument("--confirm-route-mutation", action="store_true")
    remove_parser.set_defaults(func=cmd_delete)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
