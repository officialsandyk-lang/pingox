from __future__ import annotations

from typing import Any


MULTI_BACKEND_ALGORITHMS = {
    "random",
    "round_robin",
    "weighted_round_robin",
    "ip_hash",
    "least_connections",
}


def _path(route: dict[str, Any]) -> str:
    p = str(route.get("path") or "/").strip()
    if not p.startswith("/"):
        p = "/" + p
    return p.rstrip("/") if p != "/" else "/"


def _upstream_count(route: dict[str, Any]) -> int:
    upstreams = route.get("upstreams")

    if isinstance(upstreams, list):
        return len([u for u in upstreams if u])

    if route.get("upstream") or route.get("backend") or route.get("backend_port"):
        return 1

    return 0


def validate_update_decisions(config: dict[str, Any]) -> None:
    routes = config.get("routes") or []
    seen: set[str] = set()

    for route in routes:
        if not isinstance(route, dict):
            continue

        path = _path(route)

        if path in seen:
            raise RuntimeError(f"Duplicate route blocked by decision guard: {path}")

        seen.add(path)

        algorithm = str(
            route.get("balancing")
            or route.get("algorithm")
            or "single"
        ).strip()

        count = _upstream_count(route)

        if algorithm in MULTI_BACKEND_ALGORITHMS and count < 2:
            raise RuntimeError(
                f"Invalid route decision: cannot set {path} algorithm to {algorithm} "
                f"because it has only {count} upstream. Add at least two upstreams first."
            )

    if "/" not in seen:
        raise RuntimeError("Invalid route decision: root route / is required and cannot be removed.")


def validate_prompt_algorithm_edits(active_config: dict[str, Any], edits: dict[str, str]) -> None:
    routes = active_config.get("routes") or []
    by_path = {
        _path(route): route
        for route in routes
        if isinstance(route, dict)
    }

    for path, algorithm in edits.items():
        clean_path = _path({"path": path})
        algorithm = str(algorithm or "").strip()

        route = by_path.get(clean_path)

        if not route:
            continue

        count = _upstream_count(route)

        if algorithm in MULTI_BACKEND_ALGORITHMS and count < 2:
            raise RuntimeError(
                f"Invalid route decision: cannot set {clean_path} algorithm to {algorithm} "
                f"because it has only {count} upstream. Add at least two upstreams first."
            )
