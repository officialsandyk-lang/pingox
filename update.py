from __future__ import annotations

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
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"Config not found: {CONFIG_PATH}")

    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


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


def deploy_docker_bluegreen_update(args: argparse.Namespace) -> dict[str, Any]:
    from agents.config_update_agent import apply_config_update
    from core.environment_deployer import (
        apply_profiles,
        create_backup,
        start_docker_bluegreen,
        verify_deployment,
        write_project_from_config,
    )

    prompt = " ".join(args.prompt).strip()

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
    backup = create_backup(reason="before_docker_bluegreen_update")

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

    deploy_result = start_docker_bluegreen(updated)

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


def main() -> int:
    runtime, strategy = parse_known_runtime_strategy(sys.argv[1:])
    runtime_mode = effective_runtime_mode(runtime, strategy)

    if runtime_mode == "docker_host" and strategy == "bluegreen":
        args = parse_args()

        print("🛠️ AI Pingora Gateway Docker Update")
        print(f"Runtime: {runtime}")
        print(f"Strategy: {strategy}")
        print(f"Runtime addressing mode: {runtime_mode}")

        result = deploy_docker_bluegreen_update(args)

        print("")
        print("📋 Update summary:")
        for item in result.get("change_summary", []):
            print(f"✅ {item}")

        print("")
        print(json.dumps(result, indent=2, default=str)[:6000])

        return 0 if result.get("success") else 2

    # Local/direct and old flows stay untouched.
    return delegate_to_legacy()


if __name__ == "__main__":
    raise SystemExit(main())
