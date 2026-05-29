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

    route_map = {
        _normalize_path(route.get("path")): route
        for route in routes
        if isinstance(route, dict)
    }

    expected_algorithms = {
        "/api": "random",
        "/users": "round_robin",
        "/orders": "weighted_round_robin",
        "/payments": "ip_hash",
        "/reports": "least_connections",
    }

    for path, expected in expected_algorithms.items():
        route = route_map.get(path)

        if not route:
            continue

        actual = route.get("balancing")

        if actual != expected:
            raise RuntimeError(
                f"Final config route {path} has wrong algorithm: expected {expected}, got {actual}"
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
