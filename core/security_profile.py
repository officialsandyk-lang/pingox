from __future__ import annotations

from copy import deepcopy
from typing import Any


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


DEFAULT_METHOD_RATE_LIMITS_PER_MINUTE = {
    "GET": 6000,
    "HEAD": 6000,
    "OPTIONS": 6000,
    "POST": 1200,
    "PUT": 900,
    "PATCH": 900,
    "DELETE": 300,
}


MUTATION_METHODS = ["POST", "PUT", "PATCH", "DELETE"]


def _normalize_path(path: Any) -> str:
    text = str(path or "/").strip()

    if not text.startswith("/"):
        text = "/" + text

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


def _dedupe(items: list[Any]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()

    for item in items:
        text = str(item).strip()

        if not text:
            continue

        key = text.lower()

        if key in seen:
            continue

        seen.add(key)
        output.append(text)

    return output


def apply_local_production_security_profile(
    config: dict[str, Any],
    *,
    environment: str = "local",
) -> dict[str, Any]:
    """
    Add production-grade local security policy defaults.

    Local mode does not require real WAF/OAuth/HTTPS services, but this creates
    the config contract and verifier hooks for production hardening.

    Existing runtime already enforces global allowed_methods, blocked_paths,
    body size and rate limits. Per-route/per-method/idempotency policies are
    now represented in config and can be enforced by the Rust runtime next.
    """

    fixed = deepcopy(config)

    security = fixed.setdefault("security", {})

    if not isinstance(security, dict):
        security = {}
        fixed["security"] = security

    existing_allowed = security.get("allowed_methods") or []
    security["allowed_methods"] = _dedupe(
        [str(m).upper() for m in existing_allowed] + DEFAULT_ALLOWED_METHODS
    )

    existing_blocked = security.get("blocked_paths") or []
    security["blocked_paths"] = _dedupe(
        [_normalize_path(path) for path in existing_blocked + DEFAULT_BLOCKED_PATHS]
    )

    security.setdefault("rate_limit_per_minute", 10000 if environment == "local" else 1200)
    security.setdefault("max_connections", 1000)
    security.setdefault("max_request_body_bytes", 10_485_760)
    security.setdefault("upstream_timeout_seconds", 30)

    security.setdefault(
        "https",
        {
            "enabled": False,
            "local_optional": True,
            "production_required": True,
            "termination": "waf_or_load_balancer_or_gateway",
            "redirect_http_to_https": False,
        },
    )

    security.setdefault(
        "method_rate_limits_per_minute",
        DEFAULT_METHOD_RATE_LIMITS_PER_MINUTE,
    )

    security.setdefault(
        "content_type_policy",
        {
            "enabled": True,
            "methods": ["POST", "PUT", "PATCH"],
            "allowed_content_types": ["application/json"],
            "local_enforcement_mode": "verify_config",
            "production_enforcement_mode": "gateway_or_backend",
        },
    )

    security.setdefault(
        "idempotency_policy",
        {
            "enabled": True,
            "methods": MUTATION_METHODS,
            "required_header": "Idempotency-Key",
            "local_enforcement_mode": "verify_config",
            "production_enforcement_mode": "gateway_or_backend",
        },
    )

    security.setdefault(
        "crud_probe_policy",
        {
            "enabled": True,
            "staging_safe_only": True,
            "probe_prefix": "__gateway_crud_probe__",
            "never_probe_real_destructive_business_endpoints": True,
        },
    )

    security.setdefault(
        "audit_policy",
        {
            "enabled": True,
            "methods": MUTATION_METHODS,
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
    )

    routes = fixed.get("routes") or []

    if isinstance(routes, list):
        for route in routes:
            if not isinstance(route, dict):
                continue

            if _is_static_route(route):
                continue

            route.setdefault("allowed_methods", list(DEFAULT_ALLOWED_METHODS))

            route.setdefault(
                "crud_policy",
                {
                    "enabled": True,
                    "allow_methods": list(DEFAULT_ALLOWED_METHODS),
                    "mutation_methods": list(MUTATION_METHODS),
                    "require_auth_in_production": True,
                    "require_json_content_type": True,
                    "require_idempotency_key_for_mutations": True,
                    "audit_mutations": True,
                    "staging_safe_probe_path": (
                        _normalize_path(route.get("path") or "/").rstrip("/")
                        + "/__gateway_crud_probe__/resource/123"
                    ),
                },
            )

    return fixed
