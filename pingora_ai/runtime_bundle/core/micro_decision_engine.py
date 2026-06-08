from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any
import json


MULTI_BACKEND_ALGORITHMS = {
    "random",
    "round_robin",
    "weighted_round_robin",
    "least_connections",
    "ip_hash",
}


@dataclass
class MicroDecision:
    decision_id: str
    category: str
    ok: bool
    severity: str
    expected: Any
    actual: Any
    repair_action: str | None = None
    safe_to_auto_repair: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _requested_runtime_strategy(args: Any | None, config: dict[str, Any] | None = None) -> tuple[str, str]:
    config = config or {}

    if args is not None:
        if getattr(args, "local", False):
            return "local", "direct"
        if getattr(args, "docker", False):
            return "docker_host", "bluegreen"
        if getattr(args, "linux", False):
            return "bare_metal_linux", "bluegreen"

        runtime = getattr(args, "runtime", None)
        strategy = getattr(args, "strategy", None)
        if runtime:
            if runtime in {"local", "direct"}:
                return "local", "direct"
            if runtime in {"docker", "docker_host", "docker-host"}:
                return "docker_host", "bluegreen"
            if runtime in {"linux", "bare_metal_linux", "bare-metal-linux"}:
                return "bare_metal_linux", "bluegreen"
            return str(runtime), str(strategy or config.get("strategy") or "bluegreen")

    return (
        str(config.get("runtime") or config.get("runtime_mode") or "docker_host"),
        str(config.get("strategy") or config.get("deployment_strategy") or "bluegreen"),
    )


def enforce_requested_environment(config: dict[str, Any], args: Any | None = None) -> tuple[dict[str, Any], list[MicroDecision]]:
    """
    CLI/user runtime intent is the source of truth.
    Hosted planner output is allowed to suggest but not override runtime flags.
    """
    decisions: list[MicroDecision] = []

    if not isinstance(config, dict):
        decisions.append(MicroDecision(
            decision_id="config_is_dict",
            category="config_shape",
            ok=False,
            severity="critical",
            expected="dict",
            actual=type(config).__name__,
            repair_action=None,
            safe_to_auto_repair=False,
            reason="Planner returned invalid config type.",
        ))
        return config, decisions

    expected_runtime, expected_strategy = _requested_runtime_strategy(args, config)
    actual_runtime = str(config.get("runtime") or config.get("runtime_mode") or "")
    actual_strategy = str(config.get("strategy") or config.get("deployment_strategy") or "")

    ok = actual_runtime == expected_runtime and actual_strategy == expected_strategy

    decisions.append(MicroDecision(
        decision_id="requested_runtime_strategy_matches_config",
        category="environment_selection",
        ok=ok,
        severity="critical",
        expected={"runtime": expected_runtime, "strategy": expected_strategy},
        actual={"runtime": actual_runtime, "strategy": actual_strategy},
        repair_action=None if ok else "enforce_cli_requested_runtime_strategy",
        safe_to_auto_repair=not ok,
        reason="CLI runtime flags must override hosted planner defaults.",
    ))

    if not ok:
        config["runtime"] = expected_runtime
        config["strategy"] = expected_strategy
        config["runtime_mode"] = expected_runtime
        config["deployment_strategy"] = expected_strategy

    return config, decisions


def route_micro_decisions(config: dict[str, Any]) -> list[MicroDecision]:
    decisions: list[MicroDecision] = []

    routes = config.get("routes") or []
    if not isinstance(routes, list):
        return [
            MicroDecision(
                decision_id="routes_is_list",
                category="route_shape",
                ok=False,
                severity="critical",
                expected="list",
                actual=type(routes).__name__,
                repair_action=None,
                safe_to_auto_repair=False,
                reason="Config routes must be a list.",
            )
        ]

    for route in routes:
        if not isinstance(route, dict):
            decisions.append(MicroDecision(
                decision_id="route_is_dict",
                category="route_shape",
                ok=False,
                severity="critical",
                expected="dict",
                actual=type(route).__name__,
                reason="Every route must be an object.",
            ))
            continue

        path = str(route.get("path") or "").strip() or "/"
        route_type = str(route.get("type") or route.get("route_type") or "").strip().lower()
        upstreams = route.get("upstreams") or []
        algorithm = str(route.get("balancing") or route.get("algorithm") or "single").strip().lower()

        if path == "/":
            ok = route_type == "static"
            decisions.append(MicroDecision(
                decision_id="root_route_is_static",
                category="route_shape",
                ok=ok,
                severity="critical",
                expected="static",
                actual=route_type,
                repair_action=None if ok else "force_root_route_directory_static",
                safe_to_auto_repair=not ok,
                reason="Root homepage route must be static route directory.",
            ))
            continue

        proxy_ok = route_type in {"proxy", ""}
        decisions.append(MicroDecision(
            decision_id=f"{path}_is_proxy_route",
            category="route_shape",
            ok=proxy_ok,
            severity="critical",
            expected="proxy",
            actual=route_type,
            repair_action=None if proxy_ok else "convert_or_reject_non_root_route",
            safe_to_auto_repair=False,
            reason="Non-root routes must proxy to upstreams.",
        ))

        upstream_count = len(upstreams) if isinstance(upstreams, list) else 0
        if upstream_count == 0 and route.get("upstream"):
            upstream_count = 1

        has_upstream = upstream_count >= 1
        decisions.append(MicroDecision(
            decision_id=f"{path}_has_upstream",
            category="upstream_integrity",
            ok=has_upstream,
            severity="critical",
            expected="upstream_count >= 1",
            actual=f"upstream_count = {upstream_count}",
            repair_action=None if has_upstream else "add_upstream_or_reject",
            safe_to_auto_repair=False,
            reason="Proxy routes need at least one upstream.",
        ))

        if algorithm in MULTI_BACKEND_ALGORITHMS:
            ok = upstream_count >= 2
            decisions.append(MicroDecision(
                decision_id=f"{path}_multi_backend_algorithm_has_multiple_upstreams",
                category="algorithm_integrity",
                ok=ok,
                severity="critical",
                expected="upstream_count >= 2",
                actual=f"upstream_count = {upstream_count}",
                repair_action=None if ok else "reject_or_ask_user_to_add_backend",
                safe_to_auto_repair=False,
                reason=f"{algorithm} needs at least two upstreams.",
            ))

    return decisions


def security_micro_decisions(config: dict[str, Any], production_mode: bool = False) -> list[MicroDecision]:
    security = config.get("security") if isinstance(config.get("security"), dict) else {}
    blocked = security.get("blocked_paths") or []

    required_blocked = ["/.env", "/.git", "/wp-admin", "/phpmyadmin", "/admin.php"]
    missing = [p for p in required_blocked if p not in blocked]

    decisions = [
        MicroDecision(
            decision_id="dangerous_paths_blocked",
            category="security_policy",
            ok=not missing,
            severity="high" if not production_mode else "critical",
            expected={"blocked_paths_contains": required_blocked},
            actual={"missing": missing, "blocked_paths": blocked},
            repair_action=None if not missing else "add_default_blocked_paths",
            safe_to_auto_repair=bool(missing),
            reason="Common sensitive paths must be blocked on public edge.",
        )
    ]

    return decisions


def run_micro_decisions(
    config: dict[str, Any],
    args: Any | None = None,
    production_mode: bool = False,
) -> dict[str, Any]:
    repaired_config, decisions = enforce_requested_environment(config, args)
    decisions.extend(route_micro_decisions(repaired_config))
    decisions.extend(security_micro_decisions(repaired_config, production_mode=production_mode))

    failed = [d for d in decisions if not d.ok]
    critical_failed = [d for d in failed if d.severity == "critical"]

    return {
        "success": not critical_failed,
        "ok": not critical_failed,
        "final_config": repaired_config,
        "decisions": [d.to_dict() for d in decisions],
        "failed_decisions": [d.to_dict() for d in failed],
        "critical_failed": [d.to_dict() for d in critical_failed],
    }


def explain_micro_decisions(result: dict[str, Any]) -> str:
    failed = result.get("failed_decisions") or []
    if not failed:
        return "All micro-decisions passed."

    lines = ["Micro-decision failures:"]
    for d in failed:
        lines.append(
            f"- {d.get('decision_id')}: expected={json.dumps(d.get('expected'))}, "
            f"actual={json.dumps(d.get('actual'))}, repair={d.get('repair_action')}"
        )
    return "\n".join(lines)


def run_lightweight_decisions(
    config: dict[str, Any],
    args: Any | None = None,
    operation: str = "plan",
    production_mode: bool = False,
) -> dict[str, Any]:
    """
    Lightweight decision runner.

    fast:
      runtime/strategy, route shape, upstreams, algorithms

    environment:
      fast + security policy

    deep:
      environment + all available checks
    """
    from core.decision_policy import policy_for_operation

    policy = policy_for_operation(operation)

    repaired_config, decisions = enforce_requested_environment(config, args)
    decisions.extend(route_micro_decisions(repaired_config))

    if policy.level in {"environment", "deep"}:
        decisions.extend(security_micro_decisions(repaired_config, production_mode=production_mode))

    failed = [d for d in decisions if not d.ok]
    critical_failed = [d for d in failed if d.severity == "critical"]

    return {
        "success": not critical_failed,
        "ok": not critical_failed,
        "policy": {
            "level": policy.level,
            "store_learning": policy.store_learning,
            "auto_repair": policy.auto_repair,
            "deep_scan": policy.deep_scan,
            "reason": policy.reason,
        },
        "final_config": repaired_config,
        "decisions": [d.to_dict() for d in decisions],
        "failed_decisions": [d.to_dict() for d in failed],
        "critical_failed": [d.to_dict() for d in critical_failed],
    }


def run_lightweight_decisions(
    config: dict[str, Any],
    args: Any | None = None,
    operation: str = "plan",
    production_mode: bool = False,
) -> dict[str, Any]:
    """
    Lightweight decision runner.

    fast:
      runtime/strategy, route shape, upstreams, algorithms

    environment:
      fast + security policy

    deep:
      environment + all available checks
    """
    from core.decision_policy import policy_for_operation

    policy = policy_for_operation(operation)

    repaired_config, decisions = enforce_requested_environment(config, args)
    decisions.extend(route_micro_decisions(repaired_config))

    if policy.level in {"environment", "deep"}:
        decisions.extend(security_micro_decisions(repaired_config, production_mode=production_mode))

    failed = [d for d in decisions if not d.ok]
    critical_failed = [d for d in failed if d.severity == "critical"]

    return {
        "success": not critical_failed,
        "ok": not critical_failed,
        "policy": {
            "level": policy.level,
            "store_learning": policy.store_learning,
            "auto_repair": policy.auto_repair,
            "deep_scan": policy.deep_scan,
            "reason": policy.reason,
        },
        "final_config": repaired_config,
        "decisions": [d.to_dict() for d in decisions],
        "failed_decisions": [d.to_dict() for d in failed],
        "critical_failed": [d.to_dict() for d in critical_failed],
    }
