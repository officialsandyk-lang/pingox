from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import traceback
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "reliability_reports.sqlite3"


SUPPORTED_BALANCING = {
    "random",
    "round_robin",
    "weighted_round_robin",
    "ip_hash",
    "least_connections",
}


DEFAULT_ALLOWED_METHODS = [
    "GET",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "OPTIONS",
    "HEAD",
]


DEFAULT_BLOCKED_PATHS = [
    "/.env",
    "/.git",
    "/.git/config",
    "/admin.php",
    "/phpmyadmin",
    "/wp-admin",
    "/wp-login.php",
    "/private",
    "/internal",
    "/debug",
]


LOCAL_PRODUCTION_SECURITY_DEFAULTS = {
    "allowed_methods": DEFAULT_ALLOWED_METHODS,
    "blocked_paths": DEFAULT_BLOCKED_PATHS,
    "rate_limit_per_minute": 10000,
    "max_connections": 1000,
    "max_request_body_bytes": 10_485_760,
    "upstream_timeout_seconds": 30,
    "method_rate_limits_per_minute": {
        "GET": 6000,
        "HEAD": 6000,
        "OPTIONS": 6000,
        "POST": 1200,
        "PUT": 900,
        "PATCH": 900,
        "DELETE": 300,
    },
    "https": {
        "enabled": False,
        "local_optional": True,
        "production_required": True,
        "termination": "waf_or_load_balancer_or_gateway",
        "redirect_http_to_https": False,
    },
    "content_type_policy": {
        "enabled": True,
        "methods": ["POST", "PUT", "PATCH"],
        "allowed_content_types": ["application/json"],
        "local_enforcement_mode": "verify_config",
        "production_enforcement_mode": "gateway_or_backend",
    },
    "idempotency_policy": {
        "enabled": True,
        "methods": ["POST", "PUT", "PATCH", "DELETE"],
        "required_header": "Idempotency-Key",
        "local_enforcement_mode": "verify_config",
        "production_enforcement_mode": "gateway_or_backend",
    },
    "crud_probe_policy": {
        "enabled": True,
        "staging_safe_only": True,
        "probe_prefix": "__gateway_crud_probe__",
        "never_probe_real_destructive_business_endpoints": True,
    },
    "audit_policy": {
        "enabled": True,
        "methods": ["POST", "PUT", "PATCH", "DELETE"],
        "log_path": "data/audit/crud_mutations.jsonl",
        "fields": [
            "timestamp",
            "request_id",
            "route",
            "method",
            "path",
            "status",
            "backend",
            "client_ip_hash",
        ],
        "local_enforcement_mode": "verify_config",
        "production_enforcement_mode": "gateway",
    },
}


# Deterministic repair profile for the verified local ecommerce/newbie prompt.
# This is intentionally route-specific and only applied when the known failure
# pattern is detected or when explicitly requested.
ECOMMERCE_ROUTE_PROFILE = {
    "/": {
        "path": "/",
        "type": "static",
        "root": "public",
        "index": "index.html",
    },
    "/api": {
        "path": "/api",
        "upstream": "127.0.0.1:9101",
        "backend": "127.0.0.1:9101",
        "balancing": "random",
        "upstreams": [
            "127.0.0.1:9101",
            "127.0.0.1:9102",
            "127.0.0.1:9103",
        ],
    },
    "/users": {
        "path": "/users",
        "upstream": "127.0.0.1:9101",
        "backend": "127.0.0.1:9101",
        "balancing": "round_robin",
        "upstreams": [
            "127.0.0.1:9101",
            "127.0.0.1:9102",
            "127.0.0.1:9103",
        ],
    },
    "/orders": {
        "path": "/orders",
        "upstream": "127.0.0.1:9201",
        "backend": "127.0.0.1:9201",
        "balancing": "weighted_round_robin",
        "upstreams": [
            {"address": "127.0.0.1:9201", "weight": 5},
            {"address": "127.0.0.1:9202", "weight": 2},
            {"address": "127.0.0.1:9203", "weight": 1},
        ],
    },
    "/payments": {
        "path": "/payments",
        "upstream": "127.0.0.1:9301",
        "backend": "127.0.0.1:9301",
        "balancing": "ip_hash",
        "upstreams": [
            "127.0.0.1:9301",
            "127.0.0.1:9302",
        ],
    },
    "/reports": {
        "path": "/reports",
        "upstream": "127.0.0.1:9501",
        "backend": "127.0.0.1:9501",
        "balancing": "least_connections",
        "upstreams": [
            "127.0.0.1:9501",
            "127.0.0.1:9502",
            "127.0.0.1:9503",
        ],
    },
    "/inventory": {
        "path": "/inventory",
        "upstream": "127.0.0.1:9401",
        "backend": "127.0.0.1:9401",
    },
}


@dataclass
class RootCauseClassification:
    root_cause: str
    category: str
    confidence: float
    severity: str
    summary: str
    evidence: list[str] = field(default_factory=list)
    recommended_actions: list[str] = field(default_factory=list)
    repair_action: str | None = None
    verification_level: str = "smoke"
    is_repairable: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_path(path: Any) -> str:
    text = str(path or "/").strip()

    if not text.startswith("/"):
        text = "/" + text

    text = re.sub(r"[^a-zA-Z0-9/_\-.]", "", text)

    if len(text) > 1:
        text = text.rstrip("/")

    return text or "/"


def _is_static_route(route: dict[str, Any]) -> bool:
    return str(route.get("type") or "").lower() in {
        "static",
        "web",
        "webserver",
        "web_server",
        "file_server",
        "files",
    }


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except Exception:
        return str(value)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, RootCauseClassification):
        return value.to_dict()

    if isinstance(value, dict):
        return value

    return {
        "value": str(value),
    }


def _flatten_text(value: Any) -> str:
    if value is None:
        return ""

    if isinstance(value, BaseException):
        return "".join(traceback.format_exception(type(value), value, value.__traceback__))

    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, indent=2, default=str)
        except Exception:
            return str(value)

    return str(value)


def _extract_failed_check_names(evidence: Any) -> list[str]:
    names: list[str] = []

    if isinstance(evidence, dict):
        for key in ["failed_checks", "checks"]:
            checks = evidence.get(key)

            if isinstance(checks, list):
                for check in checks:
                    if isinstance(check, dict) and check.get("ok") is False:
                        names.append(str(check.get("name") or ""))

        verification = evidence.get("verification")

        if isinstance(verification, dict):
            names.extend(_extract_failed_check_names(verification))

    return [name for name in names if name]


def _signature_for(root_cause: str, evidence: Any) -> str:
    text = _flatten_text(evidence)
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    sample = normalized[:4000]
    raw = f"{root_cause}:{sample}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:24]


def _contains_any(text: str, needles: list[str]) -> bool:
    lower = text.lower()
    return any(needle.lower() in lower for needle in needles)


def _route_by_path(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    routes = config.get("routes") or []

    output: dict[str, dict[str, Any]] = {}

    if not isinstance(routes, list):
        return output

    for route in routes:
        if not isinstance(route, dict):
            continue

        path = _normalize_path(route.get("path"))
        output[path] = route

    return output


def _extract_ports_from_upstreams(route: dict[str, Any]) -> list[str]:
    ports: list[str] = []

    upstreams = route.get("upstreams")

    if isinstance(upstreams, list):
        items = upstreams
    else:
        items = [route.get("upstream") or route.get("backend")]

    for item in items:
        if isinstance(item, dict):
            address = item.get("address") or item.get("upstream") or item.get("backend")
        else:
            address = item

        text = str(address or "")
        text = text.replace("http://", "").replace("https://", "").rstrip("/")

        if ":" not in text:
            continue

        port = text.rsplit(":", 1)[-1]

        if port.isdigit():
            ports.append(port)

    return ports


def _dedupe_strings(values: list[Any]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()

    for value in values:
        text = str(value).strip()

        if not text:
            continue

        key = text.lower()

        if key in seen:
            continue

        seen.add(key)
        output.append(text)

    return output


def classify_root_cause_object(
    evidence: Any,
    *,
    config: dict[str, Any] | None = None,
) -> RootCauseClassification:
    text = _flatten_text(evidence)
    lower = text.lower()
    failed_names = _extract_failed_check_names(evidence)
    failed_text = " ".join(failed_names).lower()

    config = config or {}

    route_map = _route_by_path(config)

    # 1. Gateway is simply not reachable.
    if (
        "status': none" in lower
        or '"status": null' in lower
        or "connection refused" in lower
        or "failed to establish a new connection" in lower
        or "homepage returns 200" in failed_text and "status" in lower and "none" in lower
    ):
        return RootCauseClassification(
            root_cause="gateway_not_running_or_not_ready",
            category="runtime",
            confidence=0.96,
            severity="high",
            summary="Gateway verification failed because the live URL was not reachable or the gateway was not ready.",
            evidence=failed_names[:20],
            recommended_actions=[
                "Start or restart the local Pingora gateway.",
                "Start demo backends before verification.",
                "Wait for Rust cold compilation to complete before declaring failure.",
                "Rerun post-change verification after the gateway returns HTTP 200.",
            ],
            repair_action="restart_gateway_backends_then_verify",
            verification_level="smoke",
            is_repairable=True,
        )

    # 2. Root route accidentally proxied to backend 3000.
    if (
        "demobackend" in lower
        and "backend_port" in lower
        and "3000" in lower
        and (
            "homepage route-directory visible" in failed_text
            or "route directory" in lower
        )
    ):
        return RootCauseClassification(
            root_cause="root_route_accidentally_generated_as_proxy",
            category="config_generation",
            confidence=0.98,
            severity="high",
            summary="The root route / is being served by a backend proxy instead of the static route-directory homepage.",
            evidence=failed_names[:20],
            recommended_actions=[
                "Remove any proxy route for /.",
                "Force / to be a static route serving public/index.html.",
                "Regenerate public/index.html and src/main.rs build_routes().",
                "Restart the gateway and run full verification.",
            ],
            repair_action="force_root_static_homepage_regenerate_restart_verify",
            verification_level="full",
            is_repairable=True,
        )

    # 3. Newbie prompt merged route upstreams/algorithms into /api or wrong paths.
    api_route = route_map.get("/api")
    payments_route = route_map.get("/payments")
    reports_route = route_map.get("/reports")

    api_ports = _extract_ports_from_upstreams(api_route or {})
    api_has_merged_ports = len(set(api_ports)) > 3 or any(
        port.startswith(("92", "93", "94", "95"))
        for port in api_ports
    )

    wrong_payments_algo = bool(payments_route) and payments_route.get("balancing") not in {None, "ip_hash"}
    wrong_reports_algo = bool(reports_route) and reports_route.get("balancing") not in {None, "least_connections"}

    if (
        api_has_merged_ports
        or wrong_payments_algo
        or wrong_reports_algo
        or "too many upstreams" in lower
        or "algorithm leak" in lower
        or "route algorithm merge" in lower
    ):
        return RootCauseClassification(
            root_cause="newbie_prompt_route_algorithm_merge",
            category="config_generation",
            confidence=0.97,
            severity="high",
            summary="The prompt/config repair merged upstreams or algorithms across routes, causing route-specific algorithm leakage.",
            evidence=failed_names[:20],
            recommended_actions=[
                "Force / as static route-directory homepage.",
                "Restore route-specific algorithms and upstreams.",
                "Ensure /api does not absorb /orders, /payments, /reports, or /inventory upstreams.",
                "Regenerate config.json, public/index.html, and src/main.rs build_routes().",
                "Restart gateway and run full verification.",
            ],
            repair_action="repair_known_ecommerce_route_profile",
            verification_level="full",
            is_repairable=True,
        )

    # 4. ip_hash sticky failed with more than one backend.
    if "ip_hash sticky" in failed_text or "ip_hash sticky" in lower:
        return RootCauseClassification(
            root_cause="ip_hash_not_sticky_or_stale_runtime",
            category="runtime_or_config",
            confidence=0.90,
            severity="medium",
            summary="An ip_hash route used multiple backends for the same local client, usually due to stale runtime or incorrect route algorithm generation.",
            evidence=failed_names[:20],
            recommended_actions=[
                "Confirm src/main.rs has balancing: \"ip_hash\" for the route.",
                "Restart the live gateway after regeneration.",
                "Rerun full verification.",
            ],
            repair_action="restart_gateway_after_regeneration",
            verification_level="full",
            is_repairable=True,
        )

    # 5. Homepage missing, but no backend 3000 evidence.
    if "homepage route-directory visible" in failed_text or "homepage contains" in failed_text:
        return RootCauseClassification(
            root_cause="route_directory_homepage_missing_or_stale",
            category="config_generation",
            confidence=0.91,
            severity="high",
            summary="The static route-directory homepage is missing, stale, or not being served from /.",
            evidence=failed_names[:20],
            recommended_actions=[
                "Regenerate public/index.html from config routes.",
                "Force / route to static route-directory homepage.",
                "Regenerate src/main.rs build_routes().",
                "Restart gateway and verify.",
            ],
            repair_action="force_root_static_homepage_regenerate_restart_verify",
            verification_level="full",
            is_repairable=True,
        )

    # 6. Blocked paths not enforced.
    if "blocked path" in failed_text or (
        "/private" in lower and "status" in lower and "200" in lower
    ):
        return RootCauseClassification(
            root_cause="blocked_paths_not_enforced_in_runtime",
            category="security_runtime",
            confidence=0.93,
            severity="high",
            summary="Blocked paths are present in policy but were still served by the runtime.",
            evidence=failed_names[:20],
            recommended_actions=[
                "Regenerate src/main.rs build_security() from config security.blocked_paths.",
                "Restart the gateway.",
                "Rerun full security verification.",
            ],
            repair_action="regenerate_build_security_restart_verify",
            verification_level="full",
            is_repairable=True,
        )

    # 7. Rust build_routes not regenerated.
    if (
        "404" in lower
        and (
            "writer-test" in lower
            or "tmpdel" in lower
            or "tmprm" in lower
            or "tmpedit" in lower
        )
    ) or "not appear in src/main.rs" in lower:
        return RootCauseClassification(
            root_cause="rust_build_routes_not_regenerated",
            category="project_writer",
            confidence=0.95,
            severity="high",
            summary="config.json and homepage were updated, but src/main.rs build_routes() did not include new routes.",
            evidence=failed_names[:20],
            recommended_actions=[
                "Rewrite src/main.rs build_routes() from config routes.",
                "Regenerate project through core.project_writer.write_project().",
                "Restart gateway and verify.",
            ],
            repair_action="rewrite_build_routes_regenerate_restart_verify",
            verification_level="full",
            is_repairable=True,
        )

    # 8. CRUD body echo missing.
    if "body_echo_verified" in lower and "false" in lower and "strict_body_echo" in lower:
        return RootCauseClassification(
            root_cause="demo_backend_does_not_echo_request_body",
            category="verification_environment",
            confidence=0.95,
            severity="medium",
            summary="CRUD forwarding probably works, but full verification cannot prove body forwarding because the demo backend did not echo the request body.",
            evidence=failed_names[:20],
            recommended_actions=[
                "Upgrade scripts/demo_backends.py to echo method, path, selected headers, and body.",
                "Restart demo backends.",
                "Rerun full verification.",
            ],
            repair_action="upgrade_demo_backend_body_echo_restart_verify",
            verification_level="full",
            is_repairable=True,
        )

    # 9. Known earlier route algorithm leakage.
    if (
        "nlp_route_algorithm_leak" in lower
        or "weighted_round_robin from /orders overwrote" in lower
        or "weighted_round_robin from orders overwrote" in lower
        or "algorithm leak" in lower
    ):
        return RootCauseClassification(
            root_cause="nlp_route_algorithm_leak",
            category="config_generation",
            confidence=0.96,
            severity="high",
            summary="A route-specific balancing algorithm leaked into unrelated routes.",
            evidence=failed_names[:20],
            recommended_actions=[
                "Canonicalize route-specific balancing per route.",
                "Preserve weighted upstream objects only for weighted routes.",
                "Regenerate project and run algorithm verification.",
            ],
            repair_action="repair_route_specific_balancing",
            verification_level="algorithm",
            is_repairable=True,
        )

    return RootCauseClassification(
        root_cause="unknown_failure",
        category="unknown",
        confidence=0.35,
        severity="medium",
        summary="The failure did not match a known root-cause pattern.",
        evidence=failed_names[:20],
        recommended_actions=[
            "Inspect post-change verifier failed_checks.",
            "Inspect generated-pingora-proxy/config.json.",
            "Inspect generated-pingora-proxy/src/main.rs build_routes() and build_security().",
            "Inspect gateway logs under generated-pingora-proxy/tmp/logs.",
        ],
        repair_action=None,
        verification_level="smoke",
        is_repairable=False,
    )


def classify_root_cause(
    evidence: Any,
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Backward-compatible dict-returning classifier.
    """
    return classify_root_cause_object(evidence, config=config).to_dict()


def detect_config_mistakes(
    config: dict[str, Any],
    *,
    evidence: Any | None = None,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

    routes = config.get("routes") or []

    if not isinstance(routes, list):
        findings.append(
            {
                "code": "routes_not_list",
                "severity": "high",
                "message": "config.routes is missing or not a list.",
                "repairable": True,
            }
        )
        return findings

    paths: dict[str, int] = {}

    for route in routes:
        if not isinstance(route, dict):
            continue

        path = _normalize_path(route.get("path"))
        paths[path] = paths.get(path, 0) + 1

    duplicate_paths = [path for path, count in paths.items() if count > 1]

    if duplicate_paths:
        findings.append(
            {
                "code": "duplicate_routes",
                "severity": "high",
                "message": "Duplicate route paths found.",
                "paths": duplicate_paths,
                "repairable": True,
            }
        )

    root_routes = [
        route
        for route in routes
        if isinstance(route, dict)
        and _normalize_path(route.get("path")) == "/"
    ]

    if not root_routes:
        findings.append(
            {
                "code": "root_static_homepage_missing",
                "severity": "high",
                "message": "/ route is missing. It should be the static route-directory homepage.",
                "repairable": True,
            }
        )
    else:
        root = root_routes[0]

        if not _is_static_route(root) or root.get("upstream") or root.get("backend") or root.get("upstreams"):
            findings.append(
                {
                    "code": "root_route_is_proxy",
                    "severity": "high",
                    "message": "/ route is a proxy or has upstreams. It must be static homepage.",
                    "route": copy.deepcopy(root),
                    "repairable": True,
                }
            )

    for route in routes:
        if not isinstance(route, dict):
            continue

        path = _normalize_path(route.get("path"))

        if path == "/" or _is_static_route(route):
            continue

        balancing = route.get("balancing")

        if balancing and balancing not in SUPPORTED_BALANCING:
            findings.append(
                {
                    "code": "unsupported_balancing",
                    "severity": "medium",
                    "message": f"Route {path} has unsupported balancing algorithm: {balancing}",
                    "path": path,
                    "balancing": balancing,
                    "repairable": True,
                }
            )

        if not route.get("upstream") and not route.get("backend") and not route.get("upstreams"):
            findings.append(
                {
                    "code": "proxy_route_missing_upstream",
                    "severity": "high",
                    "message": f"Proxy route {path} has no upstream/backend.",
                    "path": path,
                    "repairable": False,
                }
            )

    route_map = _route_by_path(config)

    api = route_map.get("/api")

    if api:
        api_ports = _extract_ports_from_upstreams(api)

        if len(set(api_ports)) > 3 or any(port.startswith(("92", "93", "94", "95")) for port in api_ports):
            findings.append(
                {
                    "code": "api_absorbed_other_route_upstreams",
                    "severity": "high",
                    "message": "/api appears to include upstreams from other routes.",
                    "path": "/api",
                    "ports": api_ports,
                    "repairable": True,
                }
            )

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
            findings.append(
                {
                    "code": "known_route_algorithm_mismatch",
                    "severity": "high",
                    "message": f"{path} should use {expected}, but config has {actual}.",
                    "path": path,
                    "expected": expected,
                    "actual": actual,
                    "repairable": True,
                }
            )

    security = config.get("security") or {}

    if not isinstance(security, dict):
        findings.append(
            {
                "code": "security_policy_missing",
                "severity": "high",
                "message": "config.security missing or invalid.",
                "repairable": True,
            }
        )
    else:
        blocked = {_normalize_path(p) for p in security.get("blocked_paths") or []}
        missing_blocked = [
            path for path in DEFAULT_BLOCKED_PATHS
            if _normalize_path(path) not in blocked
        ]

        if missing_blocked:
            findings.append(
                {
                    "code": "blocked_paths_missing",
                    "severity": "medium",
                    "message": "Some production-style blocked paths are missing.",
                    "missing": missing_blocked,
                    "repairable": True,
                }
            )

    return findings


def apply_local_production_security_defaults(config: dict[str, Any]) -> dict[str, Any]:
    fixed = copy.deepcopy(config)
    security = fixed.setdefault("security", {})

    if not isinstance(security, dict):
        security = {}
        fixed["security"] = security

    existing_allowed = [str(m).upper() for m in security.get("allowed_methods") or []]
    security["allowed_methods"] = _dedupe_strings(existing_allowed + DEFAULT_ALLOWED_METHODS)

    existing_blocked = [_normalize_path(p) for p in security.get("blocked_paths") or []]
    security["blocked_paths"] = _dedupe_strings(existing_blocked + DEFAULT_BLOCKED_PATHS)

    for key, value in LOCAL_PRODUCTION_SECURITY_DEFAULTS.items():
        if key not in security:
            security[key] = copy.deepcopy(value)

    return fixed


def force_static_homepage(config: dict[str, Any]) -> dict[str, Any]:
    fixed = copy.deepcopy(config)

    routes = fixed.get("routes") or []

    if not isinstance(routes, list):
        routes = []

    non_root_routes = [
        route for route in routes
        if not (
            isinstance(route, dict)
            and _normalize_path(route.get("path")) == "/"
        )
    ]

    root = {
        "path": "/",
        "type": "static",
        "root": "public",
        "index": "index.html",
    }

    fixed["routes"] = [root] + non_root_routes

    return fixed


def repair_known_ecommerce_route_profile(config: dict[str, Any]) -> dict[str, Any]:
    fixed = copy.deepcopy(config)
    fixed["port"] = int(fixed.get("port") or 8090)

    existing_map = _route_by_path(fixed)

    repaired_routes: list[dict[str, Any]] = []

    # Always force / as static homepage first.
    repaired_routes.append(copy.deepcopy(ECOMMERCE_ROUTE_PROFILE["/"]))

    # Repair only known paths that are present or expected in the ecommerce prompt.
    for path in ["/api", "/users", "/orders", "/payments", "/reports", "/inventory"]:
        if path in existing_map or path in ECOMMERCE_ROUTE_PROFILE:
            repaired_routes.append(copy.deepcopy(ECOMMERCE_ROUTE_PROFILE[path]))

    # Preserve extra non-root, non-ecommerce routes.
    known = set(ECOMMERCE_ROUTE_PROFILE.keys())

    for route in fixed.get("routes") or []:
        if not isinstance(route, dict):
            continue

        path = _normalize_path(route.get("path"))

        if path in known:
            continue

        repaired_routes.append(route)

    fixed["routes"] = repaired_routes

    fixed["runtime"] = fixed.get("runtime") or "local"
    fixed["runtime_mode"] = fixed.get("runtime_mode") or "local"
    fixed["strategy"] = fixed.get("strategy") or "direct"
    fixed["deployment_strategy"] = fixed.get("deployment_strategy") or "direct"

    fixed = apply_local_production_security_defaults(fixed)

    return fixed


def repair_route_specific_balancing(config: dict[str, Any]) -> dict[str, Any]:
    """
    Deterministic repair for the known route algorithm leak pattern.

    It preserves unknown routes but repairs canonical ecommerce routes if present.
    """
    fixed = copy.deepcopy(config)
    route_map = _route_by_path(fixed)

    routes: list[dict[str, Any]] = []

    for route in fixed.get("routes") or []:
        if not isinstance(route, dict):
            continue

        path = _normalize_path(route.get("path"))

        if path in ECOMMERCE_ROUTE_PROFILE:
            routes.append(copy.deepcopy(ECOMMERCE_ROUTE_PROFILE[path]))
        else:
            routes.append(route)

    if "/" not in route_map:
        routes.insert(0, copy.deepcopy(ECOMMERCE_ROUTE_PROFILE["/"]))

    fixed["routes"] = routes
    fixed = force_static_homepage(fixed)
    fixed = apply_local_production_security_defaults(fixed)

    return fixed


def repair_config_for_classification(
    config: dict[str, Any],
    classification: RootCauseClassification | dict[str, Any],
    *,
    evidence: Any | None = None,
) -> tuple[dict[str, Any], list[str]]:
    cls = _as_dict(classification)
    root_cause = str(cls.get("root_cause") or "")
    actions: list[str] = []

    repaired = copy.deepcopy(config)

    if root_cause in {
        "root_route_accidentally_generated_as_proxy",
        "route_directory_homepage_missing_or_stale",
    }:
        repaired = force_static_homepage(repaired)
        repaired = apply_local_production_security_defaults(repaired)
        actions.append("forced_root_static_homepage")
        actions.append("applied_local_production_security_defaults")

    elif root_cause in {
        "newbie_prompt_route_algorithm_merge",
    }:
        repaired = repair_known_ecommerce_route_profile(repaired)
        actions.append("repaired_known_ecommerce_route_profile")
        actions.append("forced_root_static_homepage")
        actions.append("applied_local_production_security_defaults")

    elif root_cause in {
        "nlp_route_algorithm_leak",
    }:
        repaired = repair_route_specific_balancing(repaired)
        actions.append("repaired_route_specific_balancing")
        actions.append("forced_root_static_homepage")
        actions.append("applied_local_production_security_defaults")

    elif root_cause in {
        "blocked_paths_not_enforced_in_runtime",
    }:
        repaired = apply_local_production_security_defaults(repaired)
        repaired = force_static_homepage(repaired)
        actions.append("applied_local_production_security_defaults")
        actions.append("forced_root_static_homepage")

    elif root_cause in {
        "ip_hash_not_sticky_or_stale_runtime",
        "rust_build_routes_not_regenerated",
    }:
        repaired = force_static_homepage(repaired)
        repaired = apply_local_production_security_defaults(repaired)
        actions.append("forced_root_static_homepage")
        actions.append("applied_local_production_security_defaults")
        actions.append("requires_project_writer_regeneration_and_restart")

    else:
        findings = detect_config_mistakes(repaired, evidence=evidence)

        if any(f["code"] in {"root_static_homepage_missing", "root_route_is_proxy"} for f in findings):
            repaired = force_static_homepage(repaired)
            actions.append("forced_root_static_homepage")

        if any(f.get("repairable") for f in findings):
            repaired = apply_local_production_security_defaults(repaired)
            actions.append("applied_local_production_security_defaults")

    return repaired, actions


def _ensure_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reliability_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_id TEXT UNIQUE,
                created_at TEXT NOT NULL,
                root_cause TEXT NOT NULL,
                category TEXT,
                confidence REAL,
                severity TEXT,
                summary TEXT,
                evidence_json TEXT,
                recommended_actions_json TEXT,
                repair_action TEXT,
                verification_level TEXT,
                is_repairable INTEGER,
                metadata_json TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS root_cause_learnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                root_cause TEXT NOT NULL,
                signature TEXT UNIQUE,
                successful_repair_count INTEGER DEFAULT 0,
                failed_repair_count INTEGER DEFAULT 0,
                last_repair_action TEXT,
                last_verification_level TEXT,
                notes TEXT,
                evidence_sample TEXT
            )
            """
        )

        conn.commit()


def store_reliability_report(
    classification: RootCauseClassification | dict[str, Any],
    *,
    evidence: Any | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
    incident_id: str | None = None,
) -> str:
    db_path = Path(db_path)
    _ensure_db(db_path)

    cls = _as_dict(classification)

    root_cause = str(cls.get("root_cause") or "unknown_failure")
    incident_id = incident_id or f"incident_{_utc_now().replace(':', '').replace('-', '').replace('.', '')}_{_signature_for(root_cause, evidence or cls)[:8]}"

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO reliability_reports (
                incident_id,
                created_at,
                root_cause,
                category,
                confidence,
                severity,
                summary,
                evidence_json,
                recommended_actions_json,
                repair_action,
                verification_level,
                is_repairable,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                incident_id,
                _utc_now(),
                root_cause,
                cls.get("category"),
                float(cls.get("confidence") or 0),
                cls.get("severity"),
                cls.get("summary"),
                json.dumps(cls.get("evidence") or [], default=str),
                json.dumps(cls.get("recommended_actions") or [], default=str),
                cls.get("repair_action"),
                cls.get("verification_level"),
                1 if cls.get("is_repairable") else 0,
                json.dumps(cls.get("metadata") or {}, default=str),
            ),
        )

        signature = _signature_for(root_cause, evidence or cls)

        conn.execute(
            """
            INSERT INTO root_cause_learnings (
                created_at,
                last_seen_at,
                root_cause,
                signature,
                successful_repair_count,
                failed_repair_count,
                last_repair_action,
                last_verification_level,
                notes,
                evidence_sample
            )
            VALUES (?, ?, ?, ?, 0, 0, ?, ?, ?, ?)
            ON CONFLICT(signature) DO UPDATE SET
                last_seen_at=excluded.last_seen_at,
                last_repair_action=excluded.last_repair_action,
                last_verification_level=excluded.last_verification_level,
                notes=excluded.notes,
                evidence_sample=excluded.evidence_sample
            """,
            (
                _utc_now(),
                _utc_now(),
                root_cause,
                signature,
                cls.get("repair_action"),
                cls.get("verification_level"),
                cls.get("summary"),
                _flatten_text(evidence or cls)[:4000],
            ),
        )

        conn.commit()

    return incident_id


def update_learning_repair_result(
    root_cause: str,
    *,
    evidence: Any,
    success: bool,
    repair_action: str | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> None:
    db_path = Path(db_path)
    _ensure_db(db_path)

    signature = _signature_for(root_cause, evidence)

    with sqlite3.connect(db_path) as conn:
        column = "successful_repair_count" if success else "failed_repair_count"

        conn.execute(
            f"""
            UPDATE root_cause_learnings
            SET
                last_seen_at=?,
                {column}={column}+1,
                last_repair_action=COALESCE(?, last_repair_action)
            WHERE signature=?
            """,
            (
                _utc_now(),
                repair_action,
                signature,
            ),
        )

        conn.commit()


def load_recent_learnings(
    *,
    limit: int = 20,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    db_path = Path(db_path)
    _ensure_db(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        rows = conn.execute(
            """
            SELECT *
            FROM root_cause_learnings
            ORDER BY last_seen_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return [dict(row) for row in rows]


def format_reliability_report(
    classification: RootCauseClassification | dict[str, Any],
    *,
    incident_id: str | None = None,
) -> str:
    cls = _as_dict(classification)

    lines = [
        "🧠 Root-cause reliability report",
        "",
    ]

    if incident_id:
        lines.append(f"Incident ID: {incident_id}")

    lines.extend(
        [
            f"Root cause: {cls.get('root_cause')}",
            f"Category: {cls.get('category')}",
            f"Severity: {cls.get('severity')}",
            f"Confidence: {cls.get('confidence')}",
            f"Repairable: {cls.get('is_repairable')}",
            f"Repair action: {cls.get('repair_action')}",
            f"Verification level: {cls.get('verification_level')}",
            "",
            f"Summary: {cls.get('summary')}",
        ]
    )

    evidence = cls.get("evidence") or []

    if evidence:
        lines.append("")
        lines.append("Evidence:")

        for item in evidence[:10]:
            lines.append(f"- {item}")

    actions = cls.get("recommended_actions") or []

    if actions:
        lines.append("")
        lines.append("Recommended actions:")

        for action in actions:
            lines.append(f"- {action}")

    return "\n".join(lines)


def diagnose_and_repair_config(
    config: dict[str, Any],
    *,
    evidence: Any,
    auto_repair: bool = True,
    store: bool = True,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    classification = classify_root_cause_object(evidence, config=config)
    findings = detect_config_mistakes(config, evidence=evidence)

    repaired_config = copy.deepcopy(config)
    repair_actions: list[str] = []
    changed = False

    if auto_repair and classification.is_repairable:
        repaired_config, repair_actions = repair_config_for_classification(
            config,
            classification,
            evidence=evidence,
        )
        changed = repaired_config != config

    incident_id = None

    if store:
        metadata = dict(classification.metadata or {})
        metadata["config_findings"] = findings
        metadata["repair_actions"] = repair_actions
        metadata["changed"] = changed
        classification.metadata = metadata

        incident_id = store_reliability_report(
            classification,
            evidence=evidence,
            db_path=db_path,
        )

    return {
        "success": True,
        "incident_id": incident_id,
        "classification": classification.to_dict(),
        "root_cause": classification.root_cause,
        "findings": findings,
        "repair_actions": repair_actions,
        "changed": changed,
        "repaired_config": repaired_config,
        "report": format_reliability_report(classification, incident_id=incident_id),
    }


def record_failure(
    evidence: Any,
    *,
    config: dict[str, Any] | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """
    Compatibility helper for callers that only want classification + storage.
    """
    classification = classify_root_cause_object(evidence, config=config)
    incident_id = store_reliability_report(
        classification,
        evidence=evidence,
        db_path=db_path,
    )

    return {
        "incident_id": incident_id,
        "classification": classification.to_dict(),
        "root_cause": classification.root_cause,
        "report": format_reliability_report(classification, incident_id=incident_id),
    }


if __name__ == "__main__":
    print("Root-cause agent ready.")
    print(f"SQLite DB: {DEFAULT_DB_PATH}")
    print("Recent learnings:")

    for item in load_recent_learnings(limit=10):
        print(f"- {item.get('root_cause')} | success={item.get('successful_repair_count')} | failed={item.get('failed_repair_count')}")
