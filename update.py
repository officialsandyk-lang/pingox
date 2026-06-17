from __future__ import annotations
from core.mutation_decision import enforce_mutation_decision, load_current_config

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from core.update_verification_retry import verify_deployment_with_retry


PROJECT_ROOT = Path(__file__).resolve().parent
PROJECT_DIR = PROJECT_ROOT / "generated-pingora-proxy"
CONFIG_PATH = PROJECT_DIR / "config.json"
LEGACY_UPDATE = PROJECT_ROOT / "update_local_legacy.py"

os.chdir(PROJECT_ROOT)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def normalize_runtime(value: str | None) -> str:
    text = str(value or "local").strip().lower().replace("-", "_")

    aliases = {
        "local": "local",
        "host": "local",
        "native": "local",
        "docker": "docker_host",
        "dockerhost": "docker_host",
        "docker_host": "docker_host",
        "compose": "docker_host",
    }

    return aliases.get(text, text)


def normalize_strategy(value: str | None) -> str:
    text = str(value or "direct").strip().lower().replace("-", "").replace("_", "")

    aliases = {
        "direct": "direct",
        "bluegreen": "bluegreen",
        "bg": "bluegreen",
    }

    return aliases.get(text, text)


def effective_runtime_mode(runtime: str, strategy: str) -> str:
    if runtime == "local" and strategy == "direct":
        return "local"

    if strategy == "bluegreen":
        return "docker_host"

    if runtime in {"docker", "docker_host"}:
        return "docker_host"

    return runtime


def parse_known_runtime_strategy(argv: list[str]) -> tuple[str, str]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--runtime", default="local")
    parser.add_argument("--strategy", default="direct")

    args, _unknown = parser.parse_known_args(argv)

    runtime = normalize_runtime(args.runtime)
    strategy = normalize_strategy(args.strategy)

    return runtime, strategy


def active_env_name_from_argv(argv: list[str] | None = None) -> str:
    runtime, strategy = parse_known_runtime_strategy(argv or sys.argv[1:])
    mode = effective_runtime_mode(runtime, strategy)

    if mode == "docker_host":
        return "docker"

    if mode in {"linux", "linux_systemd"}:
        return "linux"

    return "local"


def active_env_config_path(argv: list[str] | None = None) -> Path:
    from core.env_config_resolver import env_config_path

    return PROJECT_ROOT / env_config_path(active_env_name_from_argv(argv))


def delegate_to_legacy() -> int:
    if not LEGACY_UPDATE.exists():
        raise RuntimeError(f"Legacy local update entrypoint missing: {LEGACY_UPDATE}")

    print("↩️ Delegating to local legacy update flow.")
    result = subprocess.run(
        [sys.executable, str(LEGACY_UPDATE)] + sys.argv[1:],
        cwd=str(PROJECT_ROOT),
        text=True,
    )

    return int(result.returncode)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update an existing AI Pingora Gateway deployment.")

    parser.add_argument("--runtime", default="local")
    parser.add_argument("--strategy", default="direct")
    parser.add_argument(
        "--verify",
        choices=["none", "smoke", "algorithm", "crud", "stress", "full"],
        default="full",
    )
    parser.add_argument(
        "--confirm-route-mutation",
        action="store_true",
        help="Required for delete/remove route mutations.",
    )
    parser.add_argument("prompt", nargs="+")

    return parser.parse_args()


def load_config() -> dict[str, Any]:
    config_path = active_env_config_path()

    if not config_path.exists():
        if CONFIG_PATH.exists():
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        raise RuntimeError(f"Config not found: {config_path}")

    return json.loads(config_path.read_text(encoding="utf-8"))


def dangerous_route_mutation(prompt: str) -> list[str]:
    import re

    paths: list[str] = []

    for match in re.finditer(
        r"\b(?:delete|remove)\s+(?!backend\b|upstream\b)(?P<path>/[A-Za-z0-9_.\-/]+)",
        prompt or "",
        flags=re.IGNORECASE,
    ):
        path = match.group("path").rstrip("/") or "/"

        if path not in paths:
            paths.append(path)

    return paths


def confirmed(prompt: str, args: argparse.Namespace) -> bool:
    return bool(
        args.confirm_route_mutation
        or "CONFIRM ROUTE MUTATION" in str(prompt or "")
    )


def restart_demo_backends() -> None:
    script = PROJECT_ROOT / "scripts" / "demo_backends.py"

    if not script.exists():
        return

    subprocess.run(
        [sys.executable, str(script), "restart", str(CONFIG_PATH)],
        cwd=str(PROJECT_ROOT),
        text=True,
        check=False,
    )



# ---------------------------------------------------------------------
# Route preservation guard
# ---------------------------------------------------------------------


def _norm_path(path: object) -> str:
    text = str(path or "/").strip()

    if not text.startswith("/"):
        text = "/" + text

    if len(text) > 1:
        text = text.rstrip("/")

    return text or "/"


def _route_map(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    routes = config.get("routes") or []

    output: dict[str, dict[str, Any]] = {}

    if not isinstance(routes, list):
        return output

    for route in routes:
        if isinstance(route, dict):
            output[_norm_path(route.get("path"))] = route

    return output


def _upstream_signature(route: dict[str, Any]) -> list[tuple[str, int]]:
    upstreams = route.get("upstreams")

    output: list[tuple[str, int]] = []

    if isinstance(upstreams, list):
        for item in upstreams:
            if isinstance(item, dict):
                address = str(item.get("address") or item.get("upstream") or item.get("backend") or "").strip()
                weight = int(item.get("weight") or 1)
                output.append((address, weight))
            else:
                output.append((str(item).strip(), 1))

        return output

    address = route.get("upstream") or route.get("backend")

    if address:
        output.append((str(address).strip(), 1))

    return output


def _route_signature(route: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": str(route.get("type") or "proxy").lower(),
        "balancing": route.get("balancing"),
        "upstreams": _upstream_signature(route),
    }


def route_mutation_paths(prompt: str) -> set[str]:
    import re
    """
    Paths explicitly targeted by this update prompt.

    Only these routes are allowed to change.
    Every other existing route must preserve its algorithm, upstreams, and weights.
    """

    text = str(prompt or "")
    paths: set[str] = set()

    patterns = [
        r"\b(?:add|create)\s+(?P<path>/[A-Za-z0-9_.\-/]+)",
        r"\b(?:delete|remove)\s+(?!backend\b|upstream\b)(?P<path>/[A-Za-z0-9_.\-/]+)",
        r"\b(?:set|change|edit)\s+(?P<path>/[A-Za-z0-9_.\-/]+)\s+(?:algorithm|balancing|load[- ]?balancing)",
        r"\b(?:set|change|update)\s+(?P<path>/[A-Za-z0-9_.\-/]+)\s+(?:to\s+)?backend",
        r"\b(?:add|remove)\s+(?:backend|upstream)\s+[^ ]+\s+(?:to|from|for)\s+(?P<path>/[A-Za-z0-9_.\-/]+)",
        r"\b(?:balance|load[- ]?balance)\s+(?P<path>/[A-Za-z0-9_.\-/]+)",
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            paths.add(_norm_path(match.group("path")))

    return paths


def preserve_untouched_routes(
    before_config: dict[str, Any],
    after_config: dict[str, Any],
    mutable_paths: set[str],
) -> dict[str, Any]:
    """
    Preserve existing route definitions unless the user explicitly targeted that route.
    This prevents add /coupons from changing /api, /orders, /payments, etc.
    """

    before_routes = before_config.get("routes") or []
    after_routes = after_config.get("routes") or []

    before_map = _route_map(before_config)
    after_map = _route_map(after_config)

    final_routes: list[dict[str, Any]] = []

    if isinstance(before_routes, list):
        for before_route in before_routes:
            if not isinstance(before_route, dict):
                continue

            path = _norm_path(before_route.get("path"))

            if path in mutable_paths:
                # If this route was explicitly deleted, allow it to disappear.
                if path in after_map:
                    final_routes.append(after_map[path])
                continue

            # Route was not targeted. Preserve it exactly.
            final_routes.append(before_route)

    if isinstance(after_routes, list):
        for after_route in after_routes:
            if not isinstance(after_route, dict):
                continue

            path = _norm_path(after_route.get("path"))

            # New routes are allowed.
            if path not in before_map:
                final_routes.append(after_route)

    fixed = dict(after_config)
    fixed["routes"] = final_routes

    return fixed


def assert_untouched_routes_preserved(
    before_config: dict[str, Any],
    after_config: dict[str, Any],
    mutable_paths: set[str],
) -> None:
    before_map = _route_map(before_config)
    after_map = _route_map(after_config)

    failures: list[str] = []

    for path, before_route in before_map.items():
        if path in mutable_paths:
            continue

        after_route = after_map.get(path)

        if not after_route:
            failures.append(f"{path}: route disappeared")
            continue

        before_sig = _route_signature(before_route)
        after_sig = _route_signature(after_route)

        if before_sig != after_sig:
            failures.append(
                f"{path}: changed unexpectedly\n"
                f"  before={before_sig}\n"
                f"  after={after_sig}"
            )

    if failures:
        raise RuntimeError(
            "Route preservation guard failed. Untouched route(s) changed:\n"
            + "\n".join(failures)
        )


# ---------------------------------------------------------------------
# End route preservation guard
# ---------------------------------------------------------------------



# ---------------------------------------------------------------------
# Add-route duplicate / backend-port guard
# ---------------------------------------------------------------------

def _guard_normalize_path(value):
    text = str(value or "/").strip()
    if not text.startswith("/"):
        text = "/" + text
    return text.rstrip("/") if len(text) > 1 else text


def _guard_normalize_backend(value):
    text = str(value or "").strip()
    text = text.replace("http://", "").replace("https://", "").rstrip("/")
    text = text.replace("localhost:", "127.0.0.1:")
    text = text.replace("host.docker.internal:", "127.0.0.1:")
    return text


def _guard_backend_port(value):
    import re

    text = _guard_normalize_backend(value)
    match = re.search(r":(\d{2,5})\b", text)
    if not match:
        return ""

    return match.group(1)


def _guard_route_backend_addresses(route):
    addresses = []

    for key in ["upstream", "backend", "address"]:
        if route.get(key):
            addresses.append(_guard_normalize_backend(route.get(key)))

    upstreams = route.get("upstreams")

    if isinstance(upstreams, list):
        for item in upstreams:
            if isinstance(item, dict):
                for key in ["address", "upstream", "backend"]:
                    if item.get(key):
                        addresses.append(_guard_normalize_backend(item.get(key)))
            else:
                addresses.append(_guard_normalize_backend(item))

    # Remove empty/duplicates while preserving order.
    cleaned = []
    seen = set()

    for address in addresses:
        if address and address not in seen:
            cleaned.append(address)
            seen.add(address)

    return cleaned


def _guard_route_backend_ports(route):
    ports = []

    for address in _guard_route_backend_addresses(route):
        port = _guard_backend_port(address)
        if port and port not in ports:
            ports.append(port)

    return ports


def _guard_prompt_from_args(args):
    for attr in ["prompt", "update_prompt", "command", "text"]:
        value = getattr(args, attr, None)

        if isinstance(value, list):
            prompt = " ".join(str(x) for x in value).strip()
            if prompt:
                return prompt

        if isinstance(value, str) and value.strip():
            return value.strip()

    return ""


def _guard_parse_add_route_prompt(prompt):
    """
    Supports prompts like:
      add /coupons to backend 127.0.0.1:9401
      add /coupons backend 127.0.0.1:9401
      add /coupons 127.0.0.1:9401
    """
    import re

    text = str(prompt or "").strip()

    match = re.search(
        r"\badd\s+(?P<path>/[a-zA-Z0-9_\-./]*)\s+(?:to\s+)?(?:backend\s+)?(?P<backend>(?:https?://)?(?:localhost|127\.0\.0\.1|host\.docker\.internal):\d{2,5})\b",
        text,
        flags=re.IGNORECASE,
    )

    if not match:
        return None

    return {
        "path": _guard_normalize_path(match.group("path")),
        "backend": _guard_normalize_backend(match.group("backend")),
        "port": _guard_backend_port(match.group("backend")),
    }


def _guard_load_current_config():
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parent

    candidates = [
        active_env_config_path(),
        root / "generated-pingora-proxy" / "config.json",
        root / "active_config.json",
        root / "gateway_config.json",
        root / "generated-projects" / "default-project" / "current_config.json",
    ]

    for candidate in candidates:
        if not candidate.exists():
            continue

        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("routes"), list):
                return data, candidate
        except Exception:
            continue

    return None, None


def check_add_route_noop_or_backend_conflict(args):
    """
    Return a result dict when deployment should be skipped.
    Return None when update.py should continue normally.

    Add command rules:
    - same route/path + same backend exists => no-op, no deploy
    - same route/path exists with different backend => block, ask user to use edit/update
    - same route name exists => block
    - requested backend port already used by another route => block unless explicitly allowed
    """
    prompt = _guard_prompt_from_args(args)
    request = _guard_parse_add_route_prompt(prompt)

    if not request:
        return None

    config, config_path = _guard_load_current_config()

    if not config:
        return None

    routes = config.get("routes") or []

    requested_path = _guard_normalize_path(request["path"])
    requested_name = requested_path.strip("/").lower()
    requested_backend = _guard_normalize_backend(request["backend"])
    requested_port = request["port"]

    port_owner_routes = []

    for route in routes:
        if not isinstance(route, dict):
            continue

        existing_path = _guard_normalize_path(route.get("path"))
        existing_name = str(route.get("name") or existing_path.strip("/")).strip().lower()
        existing_addresses = _guard_route_backend_addresses(route)
        existing_ports = _guard_route_backend_ports(route)

        same_path = existing_path == requested_path
        same_name = bool(requested_name) and existing_name == requested_name

        # 1. Route path/name already exists.
        if same_path or same_name:
            # Exact backend/address already exists.
            if requested_backend in existing_addresses:
                return {
                    "success": True,
                    "deployed": False,
                    "changed": False,
                    "understood": True,
                    "reason": "route_already_exists_same_backend",
                    "message": (
                        f"ℹ️ Route already exists: {existing_path} -> {requested_backend}. "
                        "No deployment needed."
                    ),
                    "route": existing_path,
                    "requested_route": requested_path,
                    "backend": requested_backend,
                    "existing_backends": existing_addresses,
                    "config_path": str(config_path),
                    "change_summary": [
                        f"Route already exists: {existing_path} -> {requested_backend}"
                    ],
                }

            # Same path/name and same backend port, even if address form differs.
            if requested_port and requested_port in existing_ports:
                existing = existing_addresses[0] if existing_addresses else requested_port
                return {
                    "success": True,
                    "deployed": False,
                    "changed": False,
                    "understood": True,
                    "reason": "route_already_exists_same_port",
                    "message": (
                        f"ℹ️ Route already exists on same backend port: {existing_path} -> {existing}. "
                        "No deployment needed."
                    ),
                    "route": existing_path,
                    "requested_route": requested_path,
                    "backend": requested_backend,
                    "existing_backend": existing,
                    "existing_backends": existing_addresses,
                    "config_path": str(config_path),
                    "change_summary": [
                        f"Route already exists on same backend port: {existing_path} -> {existing}"
                    ],
                }

            # Same route path/name exists but backend is different.
            # For ADD, do not silently update. User should use edit/update.
            return {
                "success": True,
                "deployed": False,
                "changed": False,
                "understood": True,
                "reason": "route_name_already_exists_different_backend",
                "message": (
                    f"⚠️ Route already exists: {existing_path}. "
                    f"Existing backend(s): {', '.join(existing_addresses) if existing_addresses else 'unknown'}. "
                    f"Requested backend: {requested_backend}. "
                    "No deployment was made. Use `pingora-ai update` or an edit command to change the route backend."
                ),
                "route": existing_path,
                "requested_route": requested_path,
                "backend": requested_backend,
                "existing_backends": existing_addresses,
                "config_path": str(config_path),
                "change_summary": [
                    f"Route already exists with different backend: {existing_path}"
                ],
            }

        # 2. Backend port already used by another route.
        if requested_port and requested_port in existing_ports and existing_path != requested_path:
            port_owner_routes.append(
                {
                    "path": existing_path,
                    "name": existing_name,
                    "backends": existing_addresses,
                    "ports": existing_ports,
                }
            )

    # Backend port is already used by another route.
    # By default, block to prevent accidental duplicate route-to-backend mapping.
    # To allow sharing the same backend across multiple paths:
    #   PINGORA_AI_ALLOW_SHARED_BACKEND=1 pingora-ai add ...
    import os

    if port_owner_routes and os.environ.get("PINGORA_AI_ALLOW_SHARED_BACKEND") != "1":
        owner_text = ", ".join(
            f"{item['path']} -> {', '.join(item['backends'])}"
            for item in port_owner_routes
        )

        return {
            "success": True,
            "deployed": False,
            "changed": False,
            "understood": True,
            "reason": "backend_port_already_used_by_another_route",
            "message": (
                f"⚠️ Backend port {requested_port} is already used by: {owner_text}. "
                "No deployment was made. Use PINGORA_AI_ALLOW_SHARED_BACKEND=1 to allow sharing."
            ),
            "route": requested_path,
            "backend": requested_backend,
            "backend_port": requested_port,
            "existing_routes": port_owner_routes,
            "config_path": str(config_path),
            "change_summary": [
                f"Backend port {requested_port} already used by another route"
            ],
        }

    return None


# ---------------------------------------------------------------------
# End add-route duplicate / backend-port guard
# ---------------------------------------------------------------------

def _normalize_route_path_for_guard(value):
    path = str(value or "").strip()
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1:
        path = path.rstrip("/")
    return path or "/"


def _route_upstream_count_for_guard(route):
    upstreams = route.get("upstreams")

    if isinstance(upstreams, list):
        return len(upstreams)

    if route.get("upstream") or route.get("backend"):
        return 1

    return 0


def _find_route_for_guard(config, route_path):
    wanted = _normalize_route_path_for_guard(route_path)

    for route in config.get("routes") or []:
        if not isinstance(route, dict):
            continue

        if _normalize_route_path_for_guard(route.get("path")) == wanted:
            return route

    return None


def _algorithm_requires_multiple_upstreams_for_guard(algorithm):
    return str(algorithm or "").strip().lower() in {
        "random",
        "round_robin",
        "weighted_round_robin",
        "ip_hash",
        "least_connections",
    }


def _block_invalid_single_upstream_algorithm_edit(config, prompt):
    """
    Hard safety guard.

    Confirmation means: user intentionally allows a safe route mutation.
    Confirmation must not allow an invalid load-balancing algorithm.

    A route with one upstream must stay single until more upstreams exist.
    """
    import json
    import re

    prompt_text = str(prompt or "")
    lower = prompt_text.lower()

    if "algorithm" not in lower and "round_robin" not in lower and "random" not in lower and "least_connections" not in lower and "ip_hash" not in lower and "weighted_round_robin" not in lower:
        return None

    route_match = re.search(r"(/[-a-zA-Z0-9_./]+)", prompt_text)
    if not route_match:
        return None

    route_path = _normalize_route_path_for_guard(route_match.group(1))

    algorithm = None
    for candidate in [
        "weighted_round_robin",
        "least_connections",
        "round_robin",
        "random",
        "ip_hash",
        "single",
    ]:
        if candidate in lower:
            algorithm = candidate
            break

    if not algorithm:
        return None

    route = _find_route_for_guard(config, route_path)
    if not route:
        return None

    upstream_count = _route_upstream_count_for_guard(route)

    if upstream_count <= 1 and _algorithm_requires_multiple_upstreams_for_guard(algorithm):
        result = {
            "success": True,
            "deployed": False,
            "changed": False,
            "understood": True,
            "reason": "algorithm_requires_multiple_upstreams",
            "message": (
                f"⚠️ Cannot set {route_path} to {algorithm} because the route has "
                f"{upstream_count} upstream. Add at least 2 upstreams first, or keep algorithm as single. "
                "No deployment was made."
            ),
            "route": route_path,
            "requested_algorithm": algorithm,
            "upstream_count": upstream_count,
            "change_summary": [
                f"Blocked invalid algorithm edit: {route_path} has {upstream_count} upstream"
            ],
        }

        print(result["message"])
        print(json.dumps(result, indent=2))
        return result

    return None



def _common_invalid_algorithm_preflight(args) -> dict | None:
    """
    Environment-wide guard.

    Applies before Docker/local/Linux deployment. A confirmation flag confirms
    user intent, but it must not bypass load-balancer validity rules.
    """
    try:
        prompt = " ".join(args.prompt).strip()
    except Exception:
        prompt = str(getattr(args, "prompt", "") or "")

    try:
        config = load_config()
    except Exception:
        try:
            config = load_current_config()
        except Exception:
            return None

    return _block_invalid_single_upstream_algorithm_edit(config, prompt)



def deploy_docker_bluegreen_update(args: argparse.Namespace) -> dict[str, Any]:
    add_route_guard = check_add_route_noop_or_backend_conflict(args)
    if add_route_guard is not None:
        import json as _json

        print("")
        print(add_route_guard.get("message", "No deployment needed."))
        print("")
        print(_json.dumps(add_route_guard, indent=2))
        return add_route_guard

    from agents.config_update_agent import apply_config_update
    from core.environment_deployer import (
        apply_profiles,
        create_backup,
        start_docker_bluegreen, start_docker_bluegreen_transactional,
        verify_deployment,
        write_project_from_config,
    )

    prompt = " ".join(args.prompt).strip()
    # Central mutation decision gate.
    # This blocks unsafe add/edit/remove/delete operations before generation/deploy.
    enforce_mutation_decision(
        prompt=prompt,
        config=load_config(),
        confirmed=bool(getattr(args, "confirm_route_mutation", False)),
    )


    dangerous = dangerous_route_mutation(prompt)

    if dangerous and not confirmed(prompt, args):
        print("")
        print("⚠️ ROUTE UPDATE WARNING")
        print("No changes were deployed.")
        print("")
        print("This request may remove live route(s):")
        for path in dangerous:
            print(f"- {path}")
        print("")
        print("Re-run with --confirm-route-mutation to continue.")

        return {
            "success": False,
            "deployed": False,
            "requires_confirmation": True,
            "dangerous_routes": dangerous,
        }

    current = load_config()

    # Hard stop before backup/build/deploy:
    # confirmation allows intentional safe mutation, not invalid algorithms.
    invalid_algorithm_guard = _block_invalid_single_upstream_algorithm_edit(
        current,
        prompt,
    )
    if invalid_algorithm_guard is not None:
        return invalid_algorithm_guard

    backup = create_backup(reason="before_docker_bluegreen_update")

    if backup:
        try:
            from core.backup_registry import record_backup

            record_backup(
                backup_path=backup,
                reason="before_docker_bluegreen_update",
                runtime="docker_host",
                strategy="bluegreen",
                route_change=prompt,
                valid_before_backup=True,
                evidence={"stage": "after_decision_integrity_preflight"},
            )
        except Exception:
            pass

    mutable_paths = route_mutation_paths(prompt)

    current_profiled = apply_profiles(
        current,
        runtime_mode="docker_host",
        strategy="bluegreen",
    )

    update_result = apply_config_update(current, prompt)
    updated = update_result.get("config") or current

    # First preservation pass before profiles.
    updated = preserve_untouched_routes(
        current,
        updated,
        mutable_paths,
    )

    updated.setdefault("port", current.get("port", 8090))
    updated.setdefault("security", current.get("security", {}))

    updated = apply_profiles(
        updated,
        runtime_mode="docker_host",
        strategy="bluegreen",
    )

    # Second preservation pass after profiles/address rewriting.
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

    locked_paths = sorted(
        path for path in _route_map(current_profiled)
        if path not in mutable_paths
    )

    if locked_paths:
        print("🔒 Route preservation guard locked untouched routes:")
        print(", ".join(locked_paths))

    write_project_from_config(updated)
    restart_demo_backends()

    deploy_result = start_docker_bluegreen_transactional(updated)

    verification = {
        "success": True,
        "skipped": True,
        "level": "none",
    }

    if args.verify != "none":
        verification = verify_deployment_with_retry(
            updated,
            live_url=deploy_result["live_url"],
            level=args.verify,
        )

        if not verification.get("success"):
            raise RuntimeError(
                "Docker blue-green update failed verification: "
                + str(verification.get("failure_summary"))
            )

    return {
        "success": True,
        "deployed": True,
        "runtime": "docker_host",
        "runtime_mode": "docker_host",
        "strategy": "bluegreen",
        "deployment_strategy": "bluegreen",
        "live_url": deploy_result["live_url"],
        "active_color": deploy_result.get("active_color"),
        "backup": str(backup) if backup else None,
        "change_summary": update_result.get("change_summary", []),
        "changed": update_result.get("changed"),
        "understood": update_result.get("understood"),
        "verification": verification,
    }



# ---------------------------------------------------------------------
# Interrupted update rollback guard
# ---------------------------------------------------------------------

def _rollback_arg(args, name: str, default: str) -> str:
    value = getattr(args, name, None)
    return str(value or default)


def run_rollback_after_interrupted_update(args, reason: str) -> dict:
    """
    Best-effort rollback when an update is interrupted or fails mid-deploy.

    This protects against:
    - Ctrl+C during Docker blue/green update
    - docker compose conflict after old gateway was stopped
    - verification failure after generated project was changed

    Note: SIGKILL / kill -9 / WSL crash cannot be caught by Python.
    """
    import json
    import subprocess
    import sys
    from pathlib import Path

    project_root = Path(__file__).resolve().parent
    rollback_py = project_root / "rollback.py"

    runtime = _rollback_arg(args, "runtime", "docker_host")
    strategy = _rollback_arg(args, "strategy", "bluegreen")
    verify = _rollback_arg(args, "verify", "full")

    print("")
    print("🧯 Update interrupted or failed before safe completion.")
    print(f"Reason: {reason}")
    print("↩️ Attempting rollback to last known good gateway...")
    print("")

    attempts = []

    if rollback_py.exists():
        # Docker blue/green updates must roll back as Docker blue/green.
        # Do not fall back to bare rollback.py, because that defaults to local_direct
        # and can restore the wrong runtime mode.
        attempts.append([
            sys.executable,
            str(rollback_py),
            "--mode",
            "bluegreen",
            "--verify",
            verify,
        ])

    stop_script = project_root / "scripts" / "stop_docker_bluegreen.sh"

    rollback_results = []

    for command in attempts:
        try:
            print("Running rollback command:")
            print(" ".join(command))

            result = subprocess.run(
                command,
                cwd=str(project_root),
                text=True,
                capture_output=True,
                timeout=1800,
            )

            rollback_results.append(
                {
                    "command": command,
                    "returncode": result.returncode,
                    "stdout_tail": result.stdout[-4000:],
                    "stderr_tail": result.stderr[-4000:],
                }
            )

            if result.stdout.strip():
                print(result.stdout[-4000:])

            if result.stderr.strip():
                print(result.stderr[-4000:])

            if result.returncode == 0:
                print("")
                print("✅ Rollback command completed successfully.")
                return {
                    "success": False,
                    "rolled_back": True,
                    "reason": reason,
                    "rollback_command": command,
                    "rollback_results": rollback_results,
                }

        except Exception as exc:
            rollback_results.append(
                {
                    "command": command,
                    "error": str(exc),
                }
            )

    print("")
    print("⚠️ Automatic rollback command did not complete successfully.")
    print("Manual recovery command:")
    print("  python rollback.py --mode bluegreen --verify full")
    print("")
    print(json.dumps(rollback_results, indent=2, default=str)[-4000:])

    return {
        "success": False,
        "rolled_back": False,
        "reason": reason,
        "rollback_results": rollback_results,
    }


def deploy_docker_bluegreen_update_with_interrupt_rollback(args) -> dict:
    try:
        return deploy_docker_bluegreen_update(args)

    except KeyboardInterrupt:
        result = run_rollback_after_interrupted_update(
            args,
            reason="keyboard_interrupt_ctrl_c",
        )
        raise SystemExit(130 if not result.get("rolled_back") else 0)

    except Exception as exc:
        # Roll back only Docker blue/green update failures.
        runtime = _rollback_arg(args, "runtime", "docker_host")
        strategy = _rollback_arg(args, "strategy", "bluegreen")

        if runtime in {"docker", "docker_host"} and strategy == "bluegreen":
            result = run_rollback_after_interrupted_update(
                args,
                reason=f"update_exception: {exc}",
            )

            if result.get("rolled_back"):
                print("")
                print("✅ Previous stable gateway restored after failed update.")
                return result

        raise


# ---------------------------------------------------------------------
# End interrupted update rollback guard
# ---------------------------------------------------------------------

def main() -> int:
    runtime, strategy = parse_known_runtime_strategy(sys.argv[1:])
    runtime_mode = effective_runtime_mode(runtime, strategy)

    if runtime_mode == "docker_host" and strategy == "bluegreen":
        args = parse_args()

        invalid_algorithm_guard = _common_invalid_algorithm_preflight(args)
        if invalid_algorithm_guard is not None:
            print("")
            print("📋 Update summary:")
            for item in invalid_algorithm_guard.get("change_summary", []):
                print(f"⚠️ {item}")
            print("")
            print(json.dumps(invalid_algorithm_guard, indent=2, default=str)[:6000])
            return 0

        print("🛠️ AI Pingora Gateway Docker Update")
        print(f"Runtime: {runtime}")
        print(f"Strategy: {strategy}")
        print(f"Runtime addressing mode: {runtime_mode}")

        result = deploy_docker_bluegreen_update_with_interrupt_rollback(args)

        print("")
        print("📋 Update summary:")
        for item in result.get("change_summary", []):
            print(f"✅ {item}")

        print("")
        print(json.dumps(result, indent=2, default=str)[:6000])

        return 0 if result.get("success") else 2

    # Local/direct/Linux legacy flows also get the same invalid algorithm guard.
    args = parse_args()
    invalid_algorithm_guard = _common_invalid_algorithm_preflight(args)
    if invalid_algorithm_guard is not None:
        print("")
        print("📋 Update summary:")
        for item in invalid_algorithm_guard.get("change_summary", []):
            print(f"⚠️ {item}")
        print("")
        print(json.dumps(invalid_algorithm_guard, indent=2, default=str)[:6000])
        return 0

    return delegate_to_legacy()


if __name__ == "__main__":
    raise SystemExit(main())


# ---------------------------------------------------------------------------
# System decision-engine helper for update CLI
# ---------------------------------------------------------------------------
def _system_decision_validate_update_config(config: dict, prompt: str | None = None) -> dict:
    from core.system_decision_engine import apply_and_enforce_system_decisions

    return apply_and_enforce_system_decisions(
        config,
        prompt=prompt,
        strict_security=False,
        allow_repair=True,
    )
