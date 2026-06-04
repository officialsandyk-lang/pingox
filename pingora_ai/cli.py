from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from pingora_ai.api_client import plan_gateway

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


def find_project_root() -> Path:
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

    raise SystemExit(
        "Could not find project root. Run from the pingora-generator folder "
        "or set PINGORA_AI_PROJECT_ROOT."
    )


PROJECT_ROOT = find_project_root()

# Allow installed `pingora-ai` console command to import local project modules
# such as core/, agents/, orchestration/, etc.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


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

    update_py = PROJECT_ROOT / "update.py"

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
        return run_command(command, PROJECT_ROOT)

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

    health_url = "http://127.0.0.1:8000/health"

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
            "    uvicorn server.main:app --host 127.0.0.1 --port 8000 --reload\\n\\n"
            "Then retry your pingora-ai command.\\n"
        )


def cmd_plan(args: argparse.Namespace) -> int:
    ensure_planner_available_or_explain()
    prompt = join_prompt(args.prompt)

    response = plan_gateway(
        prompt=prompt,
        runtime=args.runtime,
        strategy=args.strategy,
        verify=args.verify,
    )

    if response.get("ok") and isinstance(response.get("config"), dict):
        response["config"] = ensure_route_directory_homepage(response["config"])

    output_path = PROJECT_ROOT / "data" / "api_plans" / "latest_plan.json"
    config_path = PROJECT_ROOT / "data" / "api_plans" / "latest_config.json"

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

    write_json_config(config, PROJECT_ROOT / "generated-pingora-proxy")
    write_route_directory_homepage(config, PROJECT_ROOT / "generated-pingora-proxy")

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

    plan_path = PROJECT_ROOT / "data" / "api_plans" / "latest_plan.json"
    config_path = PROJECT_ROOT / "data" / "api_plans" / "latest_config.json"

    response["config"] = ensure_route_directory_homepage(response["config"])

    save_json(plan_path, response)
    save_json(config_path, response["config"])

    print("API plan generated successfully.")
    print(f"Saved plan to: {plan_path}")
    print(f"Saved config to: {config_path}")

    if getattr(args, "target", None) in {"linux", "bare_metal_linux"}:
        return _deploy_bare_metal_from_cli(args, response["config"])

    main_py = PROJECT_ROOT / "main.py"

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

    return run_command(command, PROJECT_ROOT)


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pingora-ai",
        description=CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subcommands = parser.add_subparsers(dest="command", required=True)

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
