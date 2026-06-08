from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


MULTI_BACKEND_ALGORITHMS = {
    "random",
    "round_robin",
    "weighted_round_robin",
    "least_connections",
    "ip_hash",
}


def load_existing_config(work_dir: Path) -> dict[str, Any] | None:
    path = work_dir / "data" / "api_plans" / "latest_config.json"
    if not path.exists():
        return None

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _normalise_path(path: str) -> str:
    path = str(path or "").strip()
    if not path:
        return "/"
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1:
        path = path.rstrip("/")
    return path


def _routes_by_path(config: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(config, dict):
        return {}

    routes = config.get("routes") or []
    if not isinstance(routes, list):
        return {}

    out: dict[str, dict[str, Any]] = {}
    for route in routes:
        if isinstance(route, dict):
            out[_normalise_path(route.get("path", "/"))] = route
    return out


def detect_prompt_action(prompt: str) -> dict[str, Any]:
    text = " ".join(str(prompt or "").strip().lower().split())

    action = "unknown"
    if re.search(r"\b(add|create|new|make)\b", text):
        action = "create_or_add"
    if re.search(r"\b(change|edit|set|switch|update)\b", text):
        action = "edit"
    if re.search(r"\b(delete|remove|drop)\b", text):
        action = "remove"

    path = None
    m = re.search(r"(/[a-zA-Z0-9_\-./]+)", text)
    if m:
        path = _normalise_path(m.group(1))

    algorithm = None
    for algo in [
        "weighted_round_robin",
        "round_robin",
        "least_connections",
        "ip_hash",
        "random",
        "single",
    ]:
        if algo in text or algo.replace("_", " ") in text or algo.replace("_", "-") in text:
            algorithm = algo
            break

    # Common natural-language route without slash: "coupons route"
    if not path:
        m2 = re.search(r"\broute\s+([a-zA-Z0-9_\-]+)|\b([a-zA-Z0-9_\-]+)\s+route\b", text)
        if m2:
            candidate = m2.group(1) or m2.group(2)
            if candidate and candidate not in {"api", "gateway"}:
                path = _normalise_path(candidate)

    if not path and "api" in text:
        path = "/api"

    return {
        "action": action,
        "path": path,
        "algorithm": algorithm,
        "text": text,
    }


def apply_plan_state_guard(
    prompt: str,
    new_config: dict[str, Any],
    existing_config: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    User-side stateless-plan guard.

    - Blocks duplicate route creation.
    - Preserves existing upstreams when editing algorithm.
    - Blocks multi-backend algorithm on single-upstream route.
    - Avoids hosted planner inventing fallback 127.0.0.1:3000 during edits.
    """
    result = {
        "ok": True,
        "halt": False,
        "message": None,
        "config": new_config,
        "warnings": [],
        "decision": {},
    }

    intent = detect_prompt_action(prompt)
    result["decision"]["intent"] = intent

    path = intent.get("path")
    action = intent.get("action")
    algorithm = intent.get("algorithm")

    if not path or not isinstance(new_config, dict):
        return result

    existing_routes = _routes_by_path(existing_config)
    new_routes = _routes_by_path(new_config)
    existing_route = existing_routes.get(path)

    # Duplicate create/add should not silently generate a new plan.
    if action == "create_or_add" and existing_route:
        result["ok"] = False
        result["halt"] = True
        result["message"] = (
            f"Route {path} already exists. No new plan was created. "
            "Use edit/update with --confirm-route-mutation if you want to change it."
        )
        result["decision"]["duplicate_route"] = True
        return result

    # Edit must preserve existing route upstreams.
    if action == "edit" and existing_route:
        target_route = new_routes.get(path)

        if target_route:
            existing_upstreams = existing_route.get("upstreams") or []
            if not existing_upstreams and existing_route.get("upstream"):
                existing_upstreams = [existing_route.get("upstream")]

            if existing_upstreams:
                target_route["upstreams"] = existing_upstreams
                target_route["upstream"] = existing_upstreams[0] if isinstance(existing_upstreams[0], str) else existing_upstreams[0].get("address")

            if algorithm:
                target_route["balancing"] = algorithm

            upstream_count = len(existing_upstreams)

            if algorithm in MULTI_BACKEND_ALGORITHMS and upstream_count < 2:
                result["ok"] = False
                result["halt"] = True
                result["message"] = (
                    f"Cannot set {path} algorithm to {algorithm}: route has only "
                    f"{upstream_count} upstream. Add at least two upstreams first."
                )
                result["decision"]["algorithm_requires_multiple_upstreams"] = True
                return result

            result["warnings"].append(
                f"Preserved existing upstreams for {path} during algorithm edit."
            )

    return result
