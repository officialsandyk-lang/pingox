from __future__ import annotations

import copy
import re
from dataclasses import dataclass, asdict
from typing import Any


SUPPORTED_RUNTIME_STRATEGIES = {
    ("local", "direct"),
    ("docker_host", "bluegreen"),
    ("bare_metal_linux", "bluegreen"),
}

VALID_ALGORITHMS = {
    "single",
    "random",
    "round_robin",
    "weighted_round_robin",
    "ip_hash",
    "least_connections",
}

DEFAULT_HEALTH_CHECK = {
    "enabled": True,
    "path": "/health",
    "interval_seconds": 5,
    "timeout_seconds": 2,
    "healthy_threshold": 2,
    "unhealthy_threshold": 3,
}

CONFIRM_PHRASE = "CONFIRM ROUTE MUTATION"


@dataclass
class SystemDecision:
    ok: bool
    category: str
    decision: str
    reason: str
    severity: str = "medium"
    required_action: str | None = None
    evidence: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _path(value: Any) -> str:
    text = str(value or "/").strip()
    if not text.startswith("/"):
        text = "/" + text
    text = re.sub(r"[^a-zA-Z0-9/_\-.]", "", text)
    if len(text) > 1:
        text = text.rstrip("/")
    return text or "/"


def _runtime(value: Any) -> str:
    text = str(value or "local").strip().lower()
    if text in {"docker", "docker-host", "docker_host"}:
        return "docker_host"
    if text in {"linux", "bare-metal", "bare_metal", "bare-metal-linux", "bare_metal_linux"}:
        return "bare_metal_linux"
    if text in {"local", "direct"}:
        return "local"
    return text


def _strategy(value: Any, runtime: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return "bluegreen" if runtime in {"docker_host", "bare_metal_linux"} else "direct"
    if text in {"bluegreen", "blue-green", "blue_green"}:
        return "bluegreen"
    if text in {"direct", "local"}:
        return "direct"
    return text


def _algorithm(value: Any, upstream_count: int) -> str:
    text = str(value or "").strip().lower().replace("-", "_")

    aliases = {
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


def _address(item: Any) -> str | None:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for key in ("address", "upstream", "url", "backend"):
            if item.get(key):
                return str(item[key]).strip()
    return None


def _weight(item: Any) -> int:
    if isinstance(item, dict):
        try:
            return max(1, int(item.get("weight", 1)))
        except Exception:
            return 1
    return 1



def _legacy_upstreams_from_route(route: dict[str, Any]) -> list[Any]:
    """
    Some legacy update paths may represent a single backend as backend/backend_port
    instead of upstreams. Normalize that before decision checks.
    """
    existing = route.get("upstreams")
    if isinstance(existing, list) and existing:
        return existing

    candidates: list[Any] = []

    for key in ("backend", "upstream", "address", "target", "url"):
        value = route.get(key)
        if value:
            candidates.append(value)

    for key in ("backend_port", "port", "target_port"):
        value = route.get(key)
        if value:
            try:
                port = int(value)
                if 1 <= port <= 65535:
                    candidates.append(f"127.0.0.1:{port}")
            except Exception:
                pass

    # Handle route forms like {"backend": {"address": "..."}}
    backend_obj = route.get("backend")
    if isinstance(backend_obj, dict):
        for key in ("address", "upstream", "url", "target"):
            value = backend_obj.get(key)
            if value:
                candidates.append(value)

    # Deduplicate while preserving order.
    result: list[Any] = []
    seen = set()

    for item in candidates:
        address = _address(item)
        if not address:
            address = str(item).strip()

        if not address:
            continue

        if ":" not in address and address.isdigit():
            address = f"127.0.0.1:{address}"

        if address not in seen:
            seen.add(address)
            result.append(address)

    return result

def _decision(ok: bool, category: str, reason: str, *, decision: str | None = None, severity: str = "medium", required_action: str | None = None, evidence: dict[str, Any] | None = None) -> SystemDecision:
    return SystemDecision(
        ok=ok,
        category=category,
        decision=decision or ("allow" if ok else "repair"),
        reason=reason,
        severity=severity,
        required_action=required_action,
        evidence=evidence or {},
    )


def decide_runtime(config: dict[str, Any]) -> list[SystemDecision]:
    runtime = _runtime(config.get("runtime") or config.get("runtime_mode"))
    strategy = _strategy(config.get("strategy") or config.get("deployment_strategy"), runtime)

    ok = (runtime, strategy) in SUPPORTED_RUNTIME_STRATEGIES

    return [
        _decision(
            ok,
            "runtime_strategy",
            f"Runtime/strategy is {runtime}/{strategy}." if ok else f"Unsupported runtime/strategy {runtime}/{strategy}.",
            severity="high",
            required_action=None if ok else "Use local/direct, docker_host/bluegreen, or bare_metal_linux/bluegreen.",
            evidence={"runtime": runtime, "strategy": strategy},
        )
    ]


def decide_routes(config: dict[str, Any]) -> list[SystemDecision]:
    decisions: list[SystemDecision] = []
    routes = config.get("routes")

    if not isinstance(routes, list) or not routes:
        return [
            _decision(
                False,
                "route_config",
                "Config must contain at least one route.",
                severity="critical",
                required_action="regenerate_routes",
            )
        ]

    seen_paths: set[str] = set()

    for route in routes:
        if not isinstance(route, dict):
            decisions.append(
                _decision(
                    False,
                    "route_config",
                    "Each route must be an object.",
                    severity="critical",
                    required_action="remove_invalid_route",
                    evidence={"route": route},
                )
            )
            continue

        path = _path(route.get("path"))
        route_type = str(route.get("type") or route.get("route_type") or "proxy").lower()
        upstreams = _legacy_upstreams_from_route(route)

        if path in seen_paths:
            decisions.append(
                _decision(
                    False,
                    "route_config",
                    f"Duplicate route path found: {path}.",
                    severity="high",
                    required_action="deduplicate_routes",
                    evidence={"path": path},
                )
            )
        seen_paths.add(path)

        if path == "/":
            decisions.append(
                _decision(
                    route_type == "static",
                    "route_config",
                    "Root route must be static route-directory homepage.",
                    severity="high",
                    required_action=None if route_type == "static" else "force_root_static",
                    evidence={"path": path, "route_type": route_type},
                )
            )
            continue

        decisions.append(
            _decision(
                route_type == "proxy",
                "route_config",
                f"Non-root route {path} must be proxy.",
                severity="high",
                required_action=None if route_type == "proxy" else "convert_to_proxy_or_reject",
                evidence={"path": path, "route_type": route_type},
            )
        )

        decisions.append(
            _decision(
                bool(upstreams),
                "route_config",
                f"Proxy route {path} must have upstreams.",
                severity="critical",
                required_action=None if upstreams else "add_upstreams_or_reject",
                evidence={"path": path, "upstream_count": len(upstreams)},
            )
        )

    return decisions


def decide_algorithms(config: dict[str, Any]) -> list[SystemDecision]:
    decisions: list[SystemDecision] = []

    multi_backend_algorithms = {
        "random",
        "round_robin",
        "weighted_round_robin",
        "least_connections",
        "ip_hash",
    }

    for route in config.get("routes", []) or []:
        if not isinstance(route, dict):
            continue

        path = _path(route.get("path"))
        route_type = route.get("type") or route.get("route_type")

        # Static homepage is not a load-balanced proxy route.
        if path == "/" or route_type == "static":
            decisions.append(
                _decision(
                    True,
                    "algorithm_integrity",
                    f"{path} is static; skipping load-balancer algorithm validation.",
                    severity="low",
                    required_action=None,
                    evidence={"path": path, "route_type": route_type or "static"},
                )
            )
            continue

        upstreams = _legacy_upstreams_from_route(route)

        # Important: use the explicit requested algorithm first.
        # Do not silently downgrade random/round_robin to single when there is only one backend.
        raw_algo = str(
            route.get("balancing")
            or route.get("algorithm")
            or ""
        ).strip().lower().replace("-", "_")

        aliases = {
            "rr": "round_robin",
            "roundrobin": "round_robin",
            "weighted": "weighted_round_robin",
            "weighted_rr": "weighted_round_robin",
            "wrr": "weighted_round_robin",
            "iphash": "ip_hash",
            "least_conn": "least_connections",
            "least_connection": "least_connections",
        }

        requested_algo = aliases.get(raw_algo, raw_algo)

        if requested_algo:
            algo = requested_algo
        else:
            algo = _algorithm(route.get("balancing"), len(upstreams))

        decisions.append(
            _decision(
                algo in VALID_ALGORITHMS,
                "algorithm_integrity",
                f"{path} uses algorithm {algo}.",
                severity="high",
                required_action=None if algo in VALID_ALGORITHMS else "normalize_algorithm",
                evidence={
                    "path": path,
                    "algorithm": algo,
                    "raw_algorithm": raw_algo,
                    "upstream_count": len(upstreams),
                },
            )
        )

        # Core load-balancer knowledge rule:
        # multi-backend algorithms are not meaningful/safe on a single-upstream route.
        if algo in multi_backend_algorithms and len(upstreams) < 2:
            decisions.append(
                _decision(
                    False,
                    "algorithm_integrity",
                    (
                        f"Cannot set {path} algorithm to {algo}: "
                        f"route has only {len(upstreams)} upstream. "
                        "Add at least two upstreams first."
                    ),
                    severity="high",
                    required_action="add_at_least_two_upstreams_before_multi_backend_algorithm",
                    evidence={
                        "path": path,
                        "algorithm": algo,
                        "upstream_count": len(upstreams),
                        "upstreams": upstreams,
                        "multi_backend_algorithms": sorted(multi_backend_algorithms),
                    },
                )
            )
            continue

        if algo == "single":
            decisions.append(
                _decision(
                    len(upstreams) == 1,
                    "algorithm_integrity",
                    f"{path} single backend route must have exactly one upstream.",
                    severity="high",
                    required_action=None if len(upstreams) == 1 else "fix_single_backend_route",
                    evidence={"path": path, "upstream_count": len(upstreams)},
                )
            )

        if algo == "weighted_round_robin":
            addresses = [_address(item) for item in upstreams if _address(item)]
            weights = [_weight(item) for item in upstreams]
            duplicates = sorted({addr for addr in addresses if addresses.count(addr) > 1})
            all_weighted_objects = all(isinstance(item, dict) for item in upstreams)
            has_real_weight = any(w > 1 for w in weights)

            ok = bool(addresses) and not duplicates and all_weighted_objects and has_real_weight

            decisions.append(
                _decision(
                    ok,
                    "weighted_round_robin_integrity",
                    "Weighted route has unique weighted upstream objects." if ok else "Weighted route lost weights, duplicated upstreams, or mixed string/dict upstream shape.",
                    severity="critical",
                    required_action=None if ok else "preserve_weight_objects_or_reject",
                    evidence={
                        "path": path,
                        "addresses": addresses,
                        "weights": weights,
                        "duplicates": duplicates,
                        "all_weighted_objects": all_weighted_objects,
                        "has_real_weight": has_real_weight,
                    },
                )
            )

    return decisions


def decide_health_checks(config: dict[str, Any]) -> list[SystemDecision]:
    decisions: list[SystemDecision] = []

    global_health = config.get("health_check") if isinstance(config.get("health_check"), dict) else {}

    for route in config.get("routes", []) or []:
        if not isinstance(route, dict):
            continue

        path = _path(route.get("path"))
        if path == "/":
            continue

        health = route.get("health_check")
        if not isinstance(health, dict):
            health = global_health

        enabled = bool(health.get("enabled", True)) if isinstance(health, dict) else False
        health_path = health.get("path") if isinstance(health, dict) else None
        interval = int(health.get("interval_seconds", 0) or 0) if isinstance(health, dict) else 0
        timeout = int(health.get("timeout_seconds", 0) or 0) if isinstance(health, dict) else 0
        healthy = int(health.get("healthy_threshold", 0) or 0) if isinstance(health, dict) else 0
        unhealthy = int(health.get("unhealthy_threshold", 0) or 0) if isinstance(health, dict) else 0

        ok = (
            enabled
            and isinstance(health_path, str)
            and health_path.startswith("/")
            and interval >= 1
            and timeout >= 1
            and timeout <= interval
            and healthy >= 1
            and unhealthy >= 1
        )

        decisions.append(
            _decision(
                ok,
                "health_check",
                f"{path} has valid active health-check policy." if ok else f"{path} has missing/invalid health-check policy.",
                severity="high",
                required_action=None if ok else "apply_default_health_check",
                evidence={
                    "path": path,
                    "health_check": health,
                },
            )
        )

    return decisions


def decide_addressing(config: dict[str, Any]) -> list[SystemDecision]:
    runtime = _runtime(config.get("runtime") or config.get("runtime_mode"))
    forbidden = "127.0.0.1" if runtime == "docker_host" else "host.docker.internal"
    expected = "host.docker.internal" if runtime == "docker_host" else "127.0.0.1"

    bad = []

    for route in config.get("routes", []) or []:
        if not isinstance(route, dict):
            continue
        for item in route.get("upstreams", []) or []:
            address = _address(item)
            if address and forbidden in address:
                bad.append({"path": _path(route.get("path")), "address": address})

    return [
        _decision(
            not bad,
            "runtime_addressing",
            f"All upstreams use {runtime} addressing." if not bad else f"Some upstreams use invalid addressing for {runtime}.",
            severity="high",
            required_action=None if not bad else "rewrite_upstreams_for_runtime",
            evidence={"runtime": runtime, "expected": expected, "forbidden": forbidden, "bad": bad},
        )
    ]


def decide_security_contract(config: dict[str, Any]) -> list[SystemDecision]:
    security = config.get("security") if isinstance(config.get("security"), dict) else {}

    required = [
        "https",
        "waf",
        "auth",
        "secrets",
        "container_security",
        "scanning",
        "request_policy",
    ]

    decisions = []

    for key in required:
        ok = isinstance(security.get(key), dict) and bool(security.get(key))
        decisions.append(
            _decision(
                ok,
                "security_contract",
                f"Security contract section exists: {key}." if ok else f"Missing security contract section: {key}.",
                severity="high",
                required_action=None if ok else "apply_security_policy_contract",
                evidence={"section": key},
            )
        )

    return decisions


def decide_mutation(prompt: str) -> list[SystemDecision]:
    lower = str(prompt or "").lower()

    is_edit = any(word in lower for word in ["set ", "edit ", "change "])
    is_remove = any(word in lower for word in ["remove ", "delete "])
    requires_confirmation = is_edit or is_remove
    confirmed = CONFIRM_PHRASE in str(prompt or "")

    return [
        _decision(
            (not requires_confirmation) or confirmed,
            "route_mutation_safety",
            "Route mutation is allowed." if ((not requires_confirmation) or confirmed) else "Route mutation requires explicit confirmation.",
            severity="high",
            decision="allow" if ((not requires_confirmation) or confirmed) else "deny",
            required_action=None if ((not requires_confirmation) or confirmed) else f'Add "{CONFIRM_PHRASE}"',
            evidence={"requires_confirmation": requires_confirmation, "confirmed": confirmed},
        )
    ]


def apply_system_repairs(config: dict[str, Any]) -> dict[str, Any]:
    cfg = copy.deepcopy(config)

    runtime = _runtime(cfg.get("runtime") or cfg.get("runtime_mode"))
    strategy = _strategy(cfg.get("strategy") or cfg.get("deployment_strategy"), runtime)

    cfg["runtime"] = runtime
    cfg["runtime_mode"] = runtime
    cfg["strategy"] = strategy
    cfg["deployment_strategy"] = strategy

    routes = cfg.get("routes")
    if not isinstance(routes, list):
        routes = []

    has_root = any(isinstance(route, dict) and _path(route.get("path")) == "/" for route in routes)
    if not has_root:
        routes.insert(0, {"path": "/", "type": "static", "route_type": "static", "root": "public", "index": "index.html", "upstreams": []})

    repaired_routes = []

    for route in routes:
        if not isinstance(route, dict):
            continue

        route = copy.deepcopy(route)
        path = _path(route.get("path"))
        route["path"] = path

        if path == "/":
            route["type"] = "static"
            route["route_type"] = "static"
            route["root"] = route.get("root") or "public"
            route["index"] = route.get("index") or "index.html"
            route["upstreams"] = []
            repaired_routes.append(route)
            continue

        route["type"] = "proxy"
        route["route_type"] = "proxy"

        upstreams = _legacy_upstreams_from_route(route)
        algo = _algorithm(route.get("balancing"), len(upstreams))
        route["balancing"] = algo

        fixed_upstreams = []
        seen = set()

        for item in upstreams:
            address = _address(item)
            if not address:
                continue

            if runtime == "docker_host":
                address = address.replace("127.0.0.1", "host.docker.internal").replace("localhost", "host.docker.internal")
            else:
                address = address.replace("host.docker.internal", "127.0.0.1").replace("localhost", "127.0.0.1")

            if address in seen:
                continue
            seen.add(address)

            if algo == "weighted_round_robin":
                fixed_upstreams.append({"address": address, "weight": _weight(item)})
            else:
                fixed_upstreams.append(address)

        if algo == "single" and len(fixed_upstreams) > 1:
            fixed_upstreams = fixed_upstreams[:1]

        route["upstreams"] = fixed_upstreams

        if not isinstance(route.get("health_check"), dict):
            route["health_check"] = copy.deepcopy(DEFAULT_HEALTH_CHECK)

        repaired_routes.append(route)

    cfg["routes"] = repaired_routes
    return cfg


def evaluate_system_config(config: dict[str, Any], prompt: str | None = None, strict_security: bool = False) -> dict[str, Any]:
    decisions: list[SystemDecision] = []

    decisions.extend(decide_runtime(config))
    decisions.extend(decide_routes(config))
    decisions.extend(decide_algorithms(config))
    decisions.extend(decide_health_checks(config))
    decisions.extend(decide_addressing(config))

    if strict_security:
        decisions.extend(decide_security_contract(config))

    if prompt is not None:
        decisions.extend(decide_mutation(prompt))

    ok = all(d.ok for d in decisions)

    return {
        "ok": ok,
        "success": ok,
        "decisions": [d.to_dict() for d in decisions],
        "failed_decisions": [d.to_dict() for d in decisions if not d.ok],
        "summary": {
            "total": len(decisions),
            "failed": len([d for d in decisions if not d.ok]),
            "critical": len([d for d in decisions if not d.ok and d.severity == "critical"]),
            "high": len([d for d in decisions if not d.ok and d.severity == "high"]),
        },
    }


def enforce_system_decisions(config: dict[str, Any], *, prompt: str | None = None, strict_security: bool = False) -> dict[str, Any]:
    result = evaluate_system_config(config, prompt=prompt, strict_security=strict_security)

    if not result["ok"]:
        reason = "; ".join(item["reason"] for item in result["failed_decisions"])
        raise RuntimeError("System decision engine rejected config: " + reason)

    return result


def apply_and_enforce_system_decisions(
    config: dict[str, Any],
    *,
    prompt: str | None = None,
    strict_security: bool = False,
    allow_repair: bool = True,
) -> dict[str, Any]:
    """
    Central deterministic gate.

    Use before:
    - project write
    - deploy
    - update
    - rollback promotion
    - production check

    It repairs safe deterministic issues, then rejects unsafe config.
    It does not invent lost intent such as missing weighted-round-robin weights.
    """
    if not isinstance(config, dict):
        raise RuntimeError("System decision engine rejected config: config must be a dictionary")

    candidate = apply_system_repairs(config) if allow_repair else copy.deepcopy(config)

    decision = evaluate_system_config(
        candidate,
        prompt=prompt,
        strict_security=strict_security,
    )

    if not decision.get("ok"):
        reason = "; ".join(
            item.get("reason", "unknown")
            for item in decision.get("failed_decisions", [])
        )
        raise RuntimeError("System decision engine rejected config: " + reason)

    candidate["_system_decision_result"] = decision
    return candidate


def apply_micro_decision_gate(config: dict, args=None, production_mode: bool = False) -> dict:
    """
    Compatibility wrapper: keeps the existing high-level system decision engine,
    but adds tiny true/false micro-decisions before final config is accepted.
    """
    from core.micro_decision_engine import run_micro_decisions

    result = run_micro_decisions(config, args=args, production_mode=production_mode)
    final_config = result.get("final_config") or config
    final_config["_micro_decision_result"] = {
        "success": result.get("success"),
        "decisions": result.get("decisions", []),
        "failed_decisions": result.get("failed_decisions", []),
        "critical_failed": result.get("critical_failed", []),
    }

    if result.get("critical_failed"):
        final_config["_micro_decision_halt"] = True

    return final_config


def apply_lightweight_decision_gate(
    config: dict,
    args=None,
    operation: str = "plan",
    production_mode: bool = False,
) -> dict:
    """
    Lightweight decision gate.

    Use this instead of running every deep check every time.
    """
    from core.micro_decision_engine import run_lightweight_decisions

    result = run_lightweight_decisions(
        config,
        args=args,
        operation=operation,
        production_mode=production_mode,
    )

    final_config = result.get("final_config") or config
    final_config["_decision_policy"] = result.get("policy")
    final_config["_micro_decision_result"] = {
        "success": result.get("success"),
        "failed_decisions": result.get("failed_decisions", []),
        "critical_failed": result.get("critical_failed", []),
    }

    if result.get("critical_failed"):
        final_config["_micro_decision_halt"] = True

    return final_config
