from __future__ import annotations

import copy
import re
from dataclasses import dataclass, asdict
from typing import Any


SUPPORTED_RUNTIME_STRATEGIES = {
    ("local", "direct"),
    ("docker_host", "bluegreen"),
}

VALID_ALGORITHMS = {
    "single",
    "random",
    "round_robin",
    "weighted_round_robin",
    "ip_hash",
    "least_connections",
}

MUTATING_ACTIONS = {"edit", "delete", "remove", "replace"}

CONFIRM_PHRASE = "CONFIRM ROUTE MUTATION"


@dataclass
class Decision:
    ok: bool
    decision: str
    category: str
    reason: str
    required_action: str | None = None
    evidence: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_runtime(runtime: Any) -> str:
    value = str(runtime or "local").strip().lower()

    if value in {"docker", "docker-host", "docker_host"}:
        return "docker_host"

    if value in {"local", "direct"}:
        return "local"

    return value


def normalize_strategy(strategy: Any, runtime: str) -> str:
    value = str(strategy or "").strip().lower()

    if not value:
        return "bluegreen" if runtime == "docker_host" else "direct"

    if value in {"blue-green", "blue_green", "bluegreen"}:
        return "bluegreen"

    if value in {"local", "direct"}:
        return "direct"

    return value


def normalize_path(path: Any) -> str:
    text = str(path or "/").strip()

    if not text:
        return "/"

    if not text.startswith("/"):
        text = "/" + text

    text = re.sub(r"[^a-zA-Z0-9/_\-.]", "", text)

    if len(text) > 1:
        text = text.rstrip("/")

    return text or "/"


def normalize_algorithm(value: Any, upstream_count: int = 0) -> str:
    text = str(value or "").strip().lower().replace("-", "_")

    aliases = {
        "load_balance": "round_robin",
        "load_balancer": "round_robin",
        "rr": "round_robin",
        "roundrobin": "round_robin",
        "weighted": "weighted_round_robin",
        "weighted_rr": "weighted_round_robin",
        "wrr": "weighted_round_robin",
        "iphash": "ip_hash",
        "least_conn": "least_connections",
        "least_connection": "least_connections",
    }

    text = aliases.get(text, text)

    if text in VALID_ALGORITHMS:
        return text

    if upstream_count <= 1:
        return "single"

    return "round_robin"


def extract_upstream_address(item: Any) -> str | None:
    if isinstance(item, str):
        return item.strip()

    if isinstance(item, dict):
        for key in ("address", "upstream", "url", "backend"):
            value = item.get(key)
            if value:
                return str(value).strip()

    return None


def extract_upstream_weight(item: Any) -> int:
    if isinstance(item, dict):
        try:
            weight = int(item.get("weight", 1))
            return max(1, weight)
        except Exception:
            return 1

    return 1


def rewrite_address_for_runtime(address: str, runtime: str) -> str:
    if runtime == "docker_host":
        return address.replace("127.0.0.1", "host.docker.internal").replace("localhost", "host.docker.internal")

    return address.replace("host.docker.internal", "127.0.0.1").replace("localhost", "127.0.0.1")


def decide_runtime_strategy(runtime: Any, strategy: Any) -> Decision:
    runtime_norm = normalize_runtime(runtime)
    strategy_norm = normalize_strategy(strategy, runtime_norm)

    ok = (runtime_norm, strategy_norm) in SUPPORTED_RUNTIME_STRATEGIES

    return Decision(
        ok=ok,
        decision="allow" if ok else "deny",
        category="runtime_strategy",
        reason=(
            f"Supported runtime/strategy: {runtime_norm}/{strategy_norm}"
            if ok
            else f"Unsupported runtime/strategy: {runtime_norm}/{strategy_norm}"
        ),
        required_action=None if ok else "use local/direct or docker_host/bluegreen",
        evidence={
            "runtime": runtime_norm,
            "strategy": strategy_norm,
            "supported": sorted([f"{r}/{s}" for r, s in SUPPORTED_RUNTIME_STRATEGIES]),
        },
    )


def decide_route_shape(route: dict[str, Any]) -> list[Decision]:
    decisions: list[Decision] = []

    path = normalize_path(route.get("path"))
    route_type = str(route.get("type") or route.get("route_type") or "proxy").strip().lower()
    upstreams = route.get("upstreams") or []

    if path == "/":
        ok = route_type == "static"
        decisions.append(
            Decision(
                ok=ok,
                decision="allow" if ok else "repair",
                category="route_shape",
                reason="Root route must be static route-directory homepage.",
                required_action=None if ok else "force_root_static",
                evidence={"path": path, "route_type": route_type},
            )
        )
        return decisions

    if route_type == "static":
        decisions.append(
            Decision(
                ok=False,
                decision="deny",
                category="route_shape",
                reason="Only root route should be static in generated gateway baseline.",
                required_action="convert_non_root_static_to_proxy_or_reject",
                evidence={"path": path, "route_type": route_type},
            )
        )
        return decisions

    decisions.append(
        Decision(
            ok=bool(upstreams),
            decision="allow" if upstreams else "deny",
            category="route_shape",
            reason="Proxy route must have at least one upstream.",
            required_action=None if upstreams else "add_upstream_or_reject_route",
            evidence={"path": path, "upstream_count": len(upstreams)},
        )
    )

    return decisions


def decide_algorithm_integrity(route: dict[str, Any]) -> list[Decision]:
    path = normalize_path(route.get("path"))
    upstreams = route.get("upstreams") or []
    algorithm = normalize_algorithm(route.get("balancing"), len(upstreams))

    decisions: list[Decision] = []

    if path == "/":
        return decisions

    decisions.append(
        Decision(
            ok=algorithm in VALID_ALGORITHMS,
            decision="allow" if algorithm in VALID_ALGORITHMS else "repair",
            category="algorithm_integrity",
            reason=f"Algorithm resolved to {algorithm}.",
            required_action=None if algorithm in VALID_ALGORITHMS else "normalize_algorithm",
            evidence={"path": path, "algorithm": algorithm},
        )
    )

    if algorithm == "weighted_round_robin":
        addresses = []
        weights = []
        bad_items = []

        for item in upstreams:
            address = extract_upstream_address(item)
            weight = extract_upstream_weight(item)

            if not address:
                bad_items.append(item)
                continue

            addresses.append(address)
            weights.append(weight)

        duplicate_addresses = sorted({addr for addr in addresses if addresses.count(addr) > 1})
        has_all_weight_objects = all(isinstance(item, dict) for item in upstreams)
        has_real_weight = any(weight > 1 for weight in weights)

        ok = bool(addresses) and not duplicate_addresses and has_all_weight_objects and has_real_weight

        decisions.append(
            Decision(
                ok=ok,
                decision="allow" if ok else "repair",
                category="weighted_round_robin_integrity",
                reason=(
                    "Weighted route has unique weighted upstream objects."
                    if ok
                    else "Weighted route lost weights, has duplicate upstreams, or mixed string/dict upstream shape."
                ),
                required_action=None if ok else "preserve_weight_objects_and_remove_duplicates",
                evidence={
                    "path": path,
                    "addresses": addresses,
                    "weights": weights,
                    "duplicate_addresses": duplicate_addresses,
                    "has_all_weight_objects": has_all_weight_objects,
                    "has_real_weight": has_real_weight,
                    "bad_items": bad_items,
                },
            )
        )

    if algorithm == "single":
        decisions.append(
            Decision(
                ok=len(upstreams) == 1,
                decision="allow" if len(upstreams) == 1 else "repair",
                category="algorithm_integrity",
                reason="Single backend route must have exactly one upstream.",
                required_action=None if len(upstreams) == 1 else "reduce_to_one_upstream_or_change_algorithm",
                evidence={"path": path, "upstream_count": len(upstreams)},
            )
        )

    return decisions


def decide_addressing(config: dict[str, Any], runtime: str) -> list[Decision]:
    decisions: list[Decision] = []
    bad: list[dict[str, Any]] = []

    expected_marker = "host.docker.internal" if runtime == "docker_host" else "127.0.0.1"
    forbidden_marker = "127.0.0.1" if runtime == "docker_host" else "host.docker.internal"

    for route in config.get("routes", []) or []:
        if not isinstance(route, dict):
            continue

        path = normalize_path(route.get("path"))

        for item in route.get("upstreams", []) or []:
            address = extract_upstream_address(item)
            if not address:
                continue

            if forbidden_marker in address:
                bad.append({"path": path, "address": address})

    ok = not bad

    decisions.append(
        Decision(
            ok=ok,
            decision="allow" if ok else "repair",
            category="runtime_addressing",
            reason=(
                f"All upstreams match {runtime} addressing."
                if ok
                else f"Some upstreams use forbidden marker for {runtime}: {forbidden_marker}"
            ),
            required_action=None if ok else "rewrite_upstreams_for_runtime",
            evidence={
                "runtime": runtime,
                "expected_marker": expected_marker,
                "forbidden_marker": forbidden_marker,
                "bad_upstreams": bad,
            },
        )
    )

    return decisions


def decide_mutation_safety(prompt: str, action: str | None = None) -> Decision:
    text = str(prompt or "")
    lower = text.lower()

    detected_action = action

    if not detected_action:
        if any(word in lower for word in ["delete ", "remove "]):
            detected_action = "remove"
        elif any(word in lower for word in ["edit ", "change ", "set "]):
            detected_action = "edit"
        elif "add " in lower:
            detected_action = "add"
        else:
            detected_action = "unknown"

    requires_confirmation = detected_action in MUTATING_ACTIONS
    confirmed = CONFIRM_PHRASE in text

    ok = not requires_confirmation or confirmed

    return Decision(
        ok=ok,
        decision="allow" if ok else "deny",
        category="route_mutation_safety",
        reason=(
            "Mutation allowed."
            if ok
            else f"{detected_action} requires explicit confirmation phrase."
        ),
        required_action=None if ok else f'add "{CONFIRM_PHRASE}" to prompt',
        evidence={
            "action": detected_action,
            "requires_confirmation": requires_confirmation,
            "confirmed": confirmed,
        },
    )


def apply_deterministic_repairs(config: dict[str, Any], runtime: str) -> dict[str, Any]:
    cfg = copy.deepcopy(config)

    runtime = normalize_runtime(runtime)
    cfg["runtime"] = runtime
    cfg["runtime_mode"] = runtime

    strategy = normalize_strategy(cfg.get("strategy") or cfg.get("deployment_strategy"), runtime)
    cfg["strategy"] = strategy
    cfg["deployment_strategy"] = strategy

    routes = cfg.get("routes")
    if not isinstance(routes, list):
        routes = []

    has_root = any(isinstance(route, dict) and normalize_path(route.get("path")) == "/" for route in routes)

    if not has_root:
        routes.insert(
            0,
            {
                "path": "/",
                "type": "static",
                "route_type": "static",
                "root": "public",
                "index": "index.html",
                "balancing": "single",
                "upstreams": [],
            },
        )

    repaired_routes = []

    for route in routes:
        if not isinstance(route, dict):
            continue

        route = copy.deepcopy(route)
        path = normalize_path(route.get("path"))
        route["path"] = path

        if path == "/":
            route["type"] = "static"
            route["route_type"] = "static"
            route["root"] = route.get("root") or "public"
            route["index"] = route.get("index") or "index.html"
            route["upstreams"] = []
            repaired_routes.append(route)
            continue

        upstreams = route.get("upstreams") or []

        normalized_upstreams = []
        seen_addresses = set()

        algorithm = normalize_algorithm(route.get("balancing"), len(upstreams))
        route["balancing"] = algorithm
        route["type"] = "proxy"
        route["route_type"] = "proxy"

        for item in upstreams:
            address = extract_upstream_address(item)
            if not address:
                continue

            address = rewrite_address_for_runtime(address, runtime)

            if address in seen_addresses:
                continue

            seen_addresses.add(address)

            if algorithm == "weighted_round_robin":
                normalized_upstreams.append(
                    {
                        "address": address,
                        "weight": extract_upstream_weight(item),
                    }
                )
            else:
                normalized_upstreams.append(address)

        if algorithm == "single" and len(normalized_upstreams) > 1:
            normalized_upstreams = normalized_upstreams[:1]

        route["upstreams"] = normalized_upstreams
        repaired_routes.append(route)

    cfg["routes"] = repaired_routes
    return cfg


def evaluate_config(config: dict[str, Any]) -> dict[str, Any]:
    runtime = normalize_runtime(config.get("runtime") or config.get("runtime_mode"))
    strategy = normalize_strategy(config.get("strategy") or config.get("deployment_strategy"), runtime)

    decisions: list[Decision] = [
        decide_runtime_strategy(runtime, strategy),
    ]

    for route in config.get("routes", []) or []:
        if not isinstance(route, dict):
            decisions.append(
                Decision(
                    ok=False,
                    decision="deny",
                    category="route_shape",
                    reason="Route must be an object.",
                    required_action="remove_invalid_route",
                    evidence={"route": route},
                )
            )
            continue

        decisions.extend(decide_route_shape(route))
        decisions.extend(decide_algorithm_integrity(route))

    decisions.extend(decide_addressing(config, runtime))

    ok = all(item.ok for item in decisions)

    return {
        "ok": ok,
        "success": ok,
        "runtime": runtime,
        "strategy": strategy,
        "decisions": [item.to_dict() for item in decisions],
        "failed_decisions": [item.to_dict() for item in decisions if not item.ok],
    }


def enforce_or_raise(config: dict[str, Any]) -> dict[str, Any]:
    result = evaluate_config(config)

    if not result["ok"]:
        raise ValueError(
            "Deterministic decision engine rejected config: "
            + "; ".join(item["reason"] for item in result["failed_decisions"])
        )

    return result
