from __future__ import annotations

import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROJECT_DIR = PROJECT_ROOT / "generated-pingora-proxy"


def _normalize_path(path: Any) -> str:
    text = str(path or "/").strip()

    if not text.startswith("/"):
        text = "/" + text

    if len(text) > 1:
        text = text.rstrip("/")

    return text or "/"


def force_static_homepage(config: dict[str, Any]) -> dict[str, Any]:
    routes = config.get("routes") or []

    if not isinstance(routes, list):
        routes = []

    non_root_routes = [
        route
        for route in routes
        if not (
            isinstance(route, dict)
            and _normalize_path(route.get("path")) == "/"
        )
    ]

    config["routes"] = [
        {
            "path": "/",
            "type": "static",
            "root": "public",
            "index": "index.html",
        }
    ] + non_root_routes

    return config


def write_config_mirrors(config: dict[str, Any], project_dir: str | Path = DEFAULT_PROJECT_DIR) -> None:
    project_path = Path(project_dir)

    mirrors = [
        project_path / "config.json",
        PROJECT_ROOT / "active_config.json",
        PROJECT_ROOT / "gateway_config.json",
        PROJECT_ROOT / "generated-projects" / "default-project" / "current_config.json",
    ]

    for path in mirrors:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")


def assert_final_config_safe(config: dict[str, Any]) -> None:
    """
    Final deterministic safety check before deployment.

    This must not enforce stale historical baselines such as /api=random.
    A route with one upstream is valid as single. Multi-upstream routes must
    use a valid load-balancing algorithm.
    """
    routes = config.get("routes") or []

    if not isinstance(routes, list) or not routes:
        raise RuntimeError("Final config has no routes.")

    root = routes[0]

    if _normalize_path(root.get("path")) != "/":
        raise RuntimeError("Final config first route is not /.")

    if str(root.get("type") or "").lower() != "static":
        raise RuntimeError("Final config root / is not static.")

    if root.get("upstream") or root.get("backend") or root.get("upstreams"):
        raise RuntimeError("Final config root / still has backend/upstreams.")

    valid_multi_algorithms = {
        "random",
        "round_robin",
        "weighted_round_robin",
        "ip_hash",
        "least_connections",
    }

    for route in routes:
        if not isinstance(route, dict):
            continue

        path = _normalize_path(route.get("path"))

        if path == "/" or str(route.get("type") or "").lower() == "static":
            continue

        upstreams = route.get("upstreams") or []

        if route.get("upstream") and not upstreams:
            upstreams = [route.get("upstream")]

        if not isinstance(upstreams, list):
            raise RuntimeError(f"Final config route {path} upstreams must be a list.")

        upstream_count = len(upstreams)
        actual = route.get("balancing") or route.get("algorithm")

        if upstream_count <= 0:
            raise RuntimeError(f"Final config route {path} has no upstreams.")

        if upstream_count == 1:
            if actual not in {None, "", "single"}:
                raise RuntimeError(
                    f"Final config route {path} has wrong algorithm: expected single, got {actual}"
                )

            route["balancing"] = "single"
            route["upstream"] = upstreams[0]
            route["upstreams"] = upstreams
            continue

        if actual not in valid_multi_algorithms:
            raise RuntimeError(
                f"Final config route {path} has invalid multi-upstream algorithm: {actual}"
            )



def prepare_config_for_deployment(
    *,
    prompt: str,
    config: dict[str, Any],
    runtime: str,
    runtime_mode: str,
    strategy: str,
    project_dir: str | Path = DEFAULT_PROJECT_DIR,
    source: str = "main",
) -> dict[str, Any]:
    """
    Final source-of-truth preparation before deployment.

    This prevents the old bug where runtime verification passed against a bad
    generated config. The final config must match prompt intent first, then be
    written to Rust/project files.
    """

    from core.intent_contract import enforce_intent_contract, format_intent_contract_report
    from core.project_writer import write_project

    try:
        from core.security_profile import apply_local_production_security_profile
    except Exception:
        apply_local_production_security_profile = None

    project_path = Path(project_dir)

    intent_result = enforce_intent_contract(
        prompt=prompt,
        config=config,
        source=source,
        auto_repair=True,
        store=True,
    )

    print("")
    print(format_intent_contract_report(intent_result))

    if not intent_result.get("success"):
        failures = (intent_result.get("validation") or {}).get("failures") or []
        raise RuntimeError(
            "Prompt intent contract failed and could not be repaired: "
            + "; ".join(
                f"{item.get('path')}:{item.get('code')}"
                for item in failures[:10]
            )
        )

    final_config = intent_result["config"]

    final_config["runtime"] = runtime_mode
    final_config["runtime_mode"] = runtime_mode
    final_config["strategy"] = strategy
    final_config["deployment_strategy"] = strategy

    if apply_local_production_security_profile:
        final_config = apply_local_production_security_profile(final_config, environment=runtime)

    # Force homepage after every other repair/profile step.
    final_config = force_static_homepage(final_config)

    assert_final_config_safe(final_config)

    # This must happen after intent repair and security profile.
    write_project(final_config, project_dir=project_path)

    write_config_mirrors(final_config, project_path)

    return {
        "config": final_config,
        "intent_contract": intent_result,
        "success": True,
    }


# ---------------------------------------------------------------------------
# Security policy contract wrappers for deployment prepare functions
# ---------------------------------------------------------------------------
if not globals().get("_SECURITY_POLICY_CONTRACT_DEPLOYMENT_PREPARE_WRAPPED"):
    _SECURITY_POLICY_CONTRACT_DEPLOYMENT_PREPARE_WRAPPED = True

    from functools import wraps as _security_contract_wraps
    from core.security_policy_contract import apply_security_policy_contract as _apply_security_policy_contract

    def _apply_contract_to_prepare_result(result, environment="local"):
        if isinstance(result, dict):
            if "config" in result and isinstance(result["config"], dict):
                result = dict(result)
                result["config"] = _apply_security_policy_contract(result["config"], environment=environment)
                return result

            if "routes" in result:
                return _apply_security_policy_contract(result, environment=environment)

        return result

    def _wrap_prepare_function(name):
        fn = globals().get(name)
        if not callable(fn):
            return

        @_security_contract_wraps(fn)
        def wrapped(*args, **kwargs):
            environment = (
                kwargs.get("runtime_mode")
                or kwargs.get("runtime")
                or kwargs.get("environment")
                or "local"
            )
            result = fn(*args, **kwargs)
            return _apply_contract_to_prepare_result(result, environment=environment)

        globals()[name] = wrapped

    for _name in [
        "prepare_deployment",
        "prepare_config_for_deployment",
        "prepare_config",
        "prepare_generated_config",
        "prepare_for_deployment",
    ]:
        _wrap_prepare_function(_name)



# ---------------------------------------------------------------------------
# Bare-metal Linux preparation rules
# ---------------------------------------------------------------------------

def prepare_bare_metal_linux_config(config: dict, *, host: str | None = None, public_http_port: int = 80) -> dict:
    """
    Prevent known bare-metal issues before deployment.

    Rules:
    - Ensure / route-directory homepage exists.
    - Ensure live_url points to the Linux host, not localhost:8080.
    - Rewrite host.docker.internal to native Linux address.
    - Keep runtime/strategy explicit.
    """

    from core.homepage_route import ensure_route_directory_homepage

    config = ensure_route_directory_homepage(config)

    config["runtime"] = "bare_metal_linux"
    config["strategy"] = "bluegreen"

    if host:
        live_url = f"http://{host}/" if int(public_http_port or 80) == 80 else f"http://{host}:{public_http_port}/"
        config["live_url"] = live_url
        config["public_url"] = live_url
        config["base_url"] = live_url

    for route in config.get("routes", []) or []:
        if not isinstance(route, dict):
            continue

        if isinstance(route.get("upstreams"), list):
            route["upstreams"] = [
                str(item).replace("host.docker.internal", "127.0.0.1")
                for item in route["upstreams"]
            ]

        if route.get("upstream"):
            route["upstream"] = str(route["upstream"]).replace(
                "host.docker.internal",
                "127.0.0.1",
            )

    return config



# ---------------------------------------------------------------------------
# Shared security defaults
# ---------------------------------------------------------------------------

DEFAULT_BLOCKED_PATHS = [
    "/.env",
    "/.git",
    "/admin.php",
    "/phpmyadmin",
    "/wp-admin",
    "/wp-login.php",
]


def ensure_default_blocked_paths(config: dict) -> dict:
    if not isinstance(config, dict):
        return config

    security = config.setdefault("security", {})
    blocked = security.setdefault("blocked_paths", [])

    if not isinstance(blocked, list):
        blocked = []
        security["blocked_paths"] = blocked

    existing = set(str(item).strip() for item in blocked)

    for path in DEFAULT_BLOCKED_PATHS:
        if path not in existing:
            blocked.append(path)

    return config



def choose_readiness_url(runtime: str, config: dict | None = None) -> str:
    runtime = str(runtime or "local").lower()

    if runtime in {"local", "direct", "local_direct"}:
        return "http://127.0.0.1:8090/"

    if runtime in {"docker_host", "docker"}:
        # Docker NGINX edge may expose a dedicated edge health endpoint.
        return "http://127.0.0.1:8090/__edge_health"

    if runtime in {"bare_metal_linux", "linux"}:
        return "http://127.0.0.1/api/"

    return "http://127.0.0.1:8090/"


def repair_environment_readiness_url(runtime: str, url: str) -> str:
    runtime = str(runtime or "local").lower()
    url = str(url or "")

    if runtime in {"local", "direct", "local_direct"} and "__edge_health" in url:
        return "http://127.0.0.1:8090/"

    return url


