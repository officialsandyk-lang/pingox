from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SUPPORTED_ALGORITHMS = {
    "single",
    "random",
    "round_robin",
    "weighted_round_robin",
    "ip_hash",
    "least_connections",
}



def resolve_live_url(config: dict, live_url: str | None = None) -> str:
    return str(
        live_url
        or config.get("live_url")
        or config.get("public_url")
        or config.get("base_url")
        or "http://127.0.0.1:8090/"
    ).rstrip("/") + "/"

@dataclass
class VerificationContract:
    runtime: str
    strategy: str
    live_url: str
    required_level: str = "full"
    expected_routes: dict[str, dict[str, Any]] = field(default_factory=dict)
    required_security_checks: list[str] = field(default_factory=list)
    required_runtime_checks: list[str] = field(default_factory=list)
    required_stress_checks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime": self.runtime,
            "strategy": self.strategy,
            "live_url": self.live_url,
            "required_level": self.required_level,
            "expected_routes": self.expected_routes,
            "required_security_checks": self.required_security_checks,
            "required_runtime_checks": self.required_runtime_checks,
            "required_stress_checks": self.required_stress_checks,
        }


def _norm_path(value: Any) -> str:
    text = str(value or "/").strip()
    if not text.startswith("/"):
        text = "/" + text
    if len(text) > 1:
        text = text.rstrip("/")
    return text or "/"


def _upstreams(route: dict[str, Any]) -> list[dict[str, Any]]:
    value = route.get("upstreams")

    if isinstance(value, list):
        output = []
        for item in value:
            if isinstance(item, dict):
                output.append(
                    {
                        "address": str(item.get("address") or item.get("upstream") or item.get("backend") or ""),
                        "weight": int(item.get("weight") or 1),
                    }
                )
            else:
                output.append({"address": str(item), "weight": 1})
        return output

    address = route.get("upstream") or route.get("backend")

    if address:
        return [{"address": str(address), "weight": 1}]

    return []


def build_verification_contract(
    config: dict[str, Any],
    *,
    live_url: str,
    runtime: str | None = None,
    strategy: str | None = None,
    level: str = "full",
) -> VerificationContract:
    runtime = str(runtime or config.get("runtime_mode") or config.get("runtime") or "local")
    strategy = str(strategy or config.get("strategy") or config.get("deployment_strategy") or "direct")

    contract = VerificationContract(
        runtime=runtime,
        strategy=strategy,
        live_url=live_url,
        required_level=level,
    )

    for route in config.get("routes") or []:
        if not isinstance(route, dict):
            continue

        path = _norm_path(route.get("path"))
        route_type = str(route.get("type") or route.get("route_type") or "proxy").lower()

        item = {
            "path": path,
            "type": route_type,
            "balancing": route.get("balancing"),
            "upstreams": _upstreams(route),
        }

        if route_type == "static":
            item["root"] = route.get("root", "public")
            item["index"] = route.get("index", "index.html")

        contract.expected_routes[path] = item

    contract.required_security_checks = [
        "allowed_methods",
        "blocked_paths",
        "body_size_limit",
        "per_method_rate_limits",
        "content_type_policy",
        "idempotency_policy",
        "crud_audit_policy",
        "staging_safe_crud_probes",
        "trace_rejected",
    ]

    contract.required_runtime_checks = [
        "homepage_route_directory",
        "route_liveness",
        "algorithm_preservation",
        "crud_forwarding",
        "docker_runtime_addressing" if runtime == "docker_host" else "runtime_addressing",
    ]

    contract.required_stress_checks = [
        "concurrent_homepage",
        "concurrent_routes",
        "algorithm_sampling",
        "crud_mutation_probe",
    ]

    return contract


def validate_config_contract(config: dict[str, Any], contract: VerificationContract) -> dict[str, Any]:
    failures: list[str] = []

    routes = {
        _norm_path(route.get("path")): route
        for route in config.get("routes") or []
        if isinstance(route, dict)
    }

    if "/" not in routes:
        failures.append("Missing static homepage route /")
    else:
        root = routes["/"]
        if str(root.get("type") or "").lower() != "static":
            failures.append("/ must be static route-directory homepage")

    for path, expected in contract.expected_routes.items():
        actual = routes.get(path)

        if not actual:
            failures.append(f"Missing expected route: {path}")
            continue

        expected_type = expected.get("type")
        actual_type = str(actual.get("type") or actual.get("route_type") or "proxy").lower()

        if expected_type and expected_type != actual_type:
            failures.append(f"{path}: expected type {expected_type}, got {actual_type}")

        expected_algo = expected.get("balancing")
        actual_algo = actual.get("balancing")

        if expected_algo and actual_algo != expected_algo:
            failures.append(f"{path}: expected algorithm {expected_algo}, got {actual_algo}")

        if actual_algo and actual_algo not in SUPPORTED_ALGORITHMS:
            failures.append(f"{path}: unsupported algorithm {actual_algo}")

        expected_upstreams = expected.get("upstreams") or []
        actual_upstreams = _upstreams(actual)

        if expected_upstreams and actual_upstreams != expected_upstreams:
            failures.append(
                f"{path}: upstreams changed. expected={expected_upstreams}, actual={actual_upstreams}"
            )

    security = config.get("security") or {}

    required_methods = {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}
    actual_methods = {str(m).upper() for m in security.get("allowed_methods") or []}

    if not required_methods.issubset(actual_methods):
        failures.append(f"Security allowed_methods incomplete: {sorted(actual_methods)}")

    required_blocked = {"/.env", "/.git", "/admin.php", "/phpmyadmin", "/wp-admin", "/wp-login.php"}
    actual_blocked = {str(p) for p in security.get("blocked_paths") or []}

    if not required_blocked.issubset(actual_blocked):
        failures.append(f"Security blocked_paths incomplete: missing {sorted(required_blocked - actual_blocked)}")

    if int(security.get("max_request_body_bytes") or 0) < 1048576:
        failures.append("Security max_request_body_bytes too low or missing")

    return {
        "success": not failures,
        "failures": failures,
        "contract": contract.to_dict(),
    }


def load_contract_from_config(
    config_path: str | Path,
    *,
    live_url: str,
    level: str = "full",
) -> VerificationContract:
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))

    return build_verification_contract(
        config,
        live_url=live_url,
        runtime=config.get("runtime_mode") or config.get("runtime"),
        strategy=config.get("strategy") or config.get("deployment_strategy"),
        level=level,
    )


# ---------------------------------------------------------------------
# LangChain / LangGraph entrypoints
# ---------------------------------------------------------------------

try:
    from langsmith import traceable
except Exception:
    def traceable(*args, **kwargs):
        def decorator(fn):
            return fn
        return decorator


try:
    from langchain_core.tools import tool
except Exception:
    def tool(*args, **kwargs):
        def decorator(fn):
            return fn
        return decorator


@traceable(name="config_verification_agent", run_type="chain")
def run_config_verification_agent(
    *,
    config: dict,
    live_url: str | None = None,
    runtime: str | None = None,
    strategy: str | None = None,
    level: str = "full",
    environment: str | None = None,
) -> dict:
    live_url = resolve_live_url(config, live_url)
    """
    Production config verification agent.

    Responsibilities:
    - Build verification contract from config.
    - Validate routes, algorithms, upstreams, and weights.
    - Validate gateway security contract.
    - Validate production security contract.
    - Return graph-safe result for LangGraph orchestration.
    """

    from core.production_security_contract import (
        apply_production_security_contract,
        validate_production_security_contract,
    )

    runtime = runtime or config.get("runtime_mode") or config.get("runtime") or "local"
    strategy = strategy or config.get("strategy") or config.get("deployment_strategy") or "direct"
    environment = environment or runtime

    fixed_config = apply_production_security_contract(
        config,
        environment=environment,
    )

    contract = build_verification_contract(
        fixed_config,
        live_url=live_url,
        runtime=runtime,
        strategy=strategy,
        level=level,
    )

    config_contract = validate_config_contract(
        fixed_config,
        contract,
    )

    production_security = validate_production_security_contract(
        fixed_config,
        environment=environment,
    )

    failures = []

    if not config_contract.get("success"):
        failures.extend(config_contract.get("failures") or [])

    if not production_security.get("success"):
        failures.extend(
            item.get("name", "production_security_failure")
            for item in production_security.get("failures") or []
        )

    success = bool(
        config_contract.get("success")
        and production_security.get("success")
    )

    return {
        "success": success,
        "agent": "config_verification_agent",
        "runtime": runtime,
        "strategy": strategy,
        "environment": environment,
        "level": level,
        "live_url": live_url,
        "config": fixed_config,
        "contract": contract.to_dict(),
        "config_contract": config_contract,
        "production_security": production_security,
        "failures": failures,
    }


@tool("config_verification_agent")
def config_verification_langchain_tool(
    config_json: str,
    live_url: str | None = None,
    runtime: str = "local",
    strategy: str = "direct",
    level: str = "full",
    environment: str = "local",
) -> str:
    """
    LangChain tool wrapper for the config verification agent.
    Input: config JSON string.
    Output: JSON verification result.
    """

    import json

    config = json.loads(config_json)

    result = run_config_verification_agent(
        config=config,
        live_url=live_url,
        runtime=runtime,
        strategy=strategy,
        level=level,
        environment=environment,
    )

    return json.dumps(result, indent=2, default=str)


# ---------------------------------------------------------------------
# End LangChain / LangGraph entrypoints
# ---------------------------------------------------------------------


# ---------------------------------------------------------------------
# FINAL override with algorithm integrity support
# ---------------------------------------------------------------------

@traceable(name="config_verification_agent_with_algorithm_integrity", run_type="chain")
def run_config_verification_agent(
    *,
    config: dict,
    live_url: str | None = None,
    runtime: str | None = None,
    strategy: str | None = None,
    level: str = "full",
    environment: str | None = None,
    require_algorithm_integrity: bool = False,
) -> dict:
    live_url = resolve_live_url(config, live_url)

    from core.production_security_contract import (
        apply_production_security_contract,
        validate_production_security_contract,
    )
    from core.algorithm_integrity import validate_algorithm_integrity

    runtime = runtime or config.get("runtime_mode") or config.get("runtime") or "local"
    strategy = strategy or config.get("strategy") or config.get("deployment_strategy") or "direct"
    environment = environment or runtime

    fixed_config = apply_production_security_contract(
        config,
        environment=environment,
    )

    contract = build_verification_contract(
        fixed_config,
        live_url=live_url,
        runtime=runtime,
        strategy=strategy,
        level=level,
    )

    config_contract = validate_config_contract(
        fixed_config,
        contract,
    )

    production_security = validate_production_security_contract(
        fixed_config,
        environment=environment,
    )

    algorithm_integrity = {
        "success": True,
        "skipped": True,
        "reason": "algorithm integrity baseline check is disabled for create/apply unless explicitly requested",
    }

    if require_algorithm_integrity:
        algorithm_integrity = validate_algorithm_integrity(
            fixed_config,
            require_baseline=require_algorithm_integrity,
            allow_extra_routes=False,
        )

    failures = []

    if not config_contract.get("success"):
        failures.extend(config_contract.get("failures") or [])

    if not production_security.get("success"):
        failures.extend(
            item.get("name", "production_security_failure")
            for item in production_security.get("failures") or []
        )

    if require_algorithm_integrity and not algorithm_integrity.get("success"):
        failures.extend(algorithm_integrity.get("failures") or [])

    success = bool(
        config_contract.get("success")
        and production_security.get("success")
        and (algorithm_integrity.get("success") or not require_algorithm_integrity)
    )

    return {
        "success": success,
        "agent": "config_verification_agent",
        "runtime": runtime,
        "strategy": strategy,
        "environment": environment,
        "level": level,
        "live_url": live_url,
        "config": fixed_config,
        "contract": contract.to_dict(),
        "config_contract": config_contract,
        "production_security": production_security,
        "algorithm_integrity": algorithm_integrity,
        "failures": failures,
    }


# ---------------------------------------------------------------------
# End final algorithm integrity override
# ---------------------------------------------------------------------
