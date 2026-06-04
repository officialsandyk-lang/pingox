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


def apply_runtime_shortcuts(args: argparse.Namespace) -> None:
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
) -> int:
    if confirm_route_mutation and "CONFIRM ROUTE MUTATION" not in prompt:
        prompt = prompt + " CONFIRM ROUTE MUTATION"

    if runtime in {"bare_metal_linux", "linux"}:
        raise SystemExit(
            "Bare-metal add/edit/remove is not wired yet. "
            "Use pingora-ai apply --linux for now, or implement a bare-metal mutation path "
            "that updates config and calls deploy_bare_metal_with_shared_pipeline()."
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

        response["config"] = enforce_requested_environment(response["config"], args)
        response["config"] = apply_lightweight_decision_gate(
            response["config"],
            args=args,
            operation="plan",
            production_mode=False,
        )
        response["config"] = ensure_route_directory_homepage(response["config"])

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

    response["config"] = ensure_route_directory_homepage(response["config"])

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
