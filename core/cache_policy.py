from __future__ import annotations

from typing import Any


CACHE_ALLOWED_METHODS = {"GET", "HEAD"}
CACHE_ALLOWED_KEYS = {
    "enabled",
    "ttl_seconds",
    "methods",
    "cache_key",
    "bypass_headers",
    "stale_while_revalidate_seconds",
}

DEFAULT_CACHE_POLICY = {
    "enabled": False,
    "ttl_seconds": 30,
    "methods": ["GET"],
    "cache_key": "method:path:query",
    "bypass_headers": ["authorization", "x-api-key"],
    "stale_while_revalidate_seconds": 0,
}


def normalize_cache_policy(value: Any) -> dict[str, Any]:
    """
    Normalize one route cache policy.

    Safe local-only contract:
    - cache defaults to disabled
    - only GET/HEAD cacheable for now
    - auth/API-key headers bypass by default
    - no runtime mutation here
    """

    if value is None:
        return dict(DEFAULT_CACHE_POLICY)

    if not isinstance(value, dict):
        return dict(DEFAULT_CACHE_POLICY)

    policy = dict(DEFAULT_CACHE_POLICY)

    enabled = value.get("enabled", False)
    policy["enabled"] = bool(enabled)

    try:
        ttl = int(value.get("ttl_seconds", policy["ttl_seconds"]))
    except Exception:
        ttl = policy["ttl_seconds"]

    if ttl < 1:
        ttl = 1
    if ttl > 86400:
        ttl = 86400

    policy["ttl_seconds"] = ttl

    methods = value.get("methods", policy["methods"])
    if not isinstance(methods, list):
        methods = policy["methods"]

    normalized_methods: list[str] = []
    for method in methods:
        text = str(method or "").strip().upper()
        if text in CACHE_ALLOWED_METHODS and text not in normalized_methods:
            normalized_methods.append(text)

    if not normalized_methods:
        normalized_methods = ["GET"]

    policy["methods"] = normalized_methods

    cache_key = str(value.get("cache_key", policy["cache_key"]) or "").strip()
    if cache_key not in {"method:path:query", "method:path"}:
        cache_key = "method:path:query"

    policy["cache_key"] = cache_key

    bypass_headers = value.get("bypass_headers", policy["bypass_headers"])
    if not isinstance(bypass_headers, list):
        bypass_headers = policy["bypass_headers"]

    normalized_headers: list[str] = []
    for header in bypass_headers:
        text = str(header or "").strip().lower()
        if text and text not in normalized_headers:
            normalized_headers.append(text)

    for required in ["authorization", "x-api-key"]:
        if required not in normalized_headers:
            normalized_headers.append(required)

    policy["bypass_headers"] = normalized_headers

    try:
        stale = int(
            value.get(
                "stale_while_revalidate_seconds",
                policy["stale_while_revalidate_seconds"],
            )
        )
    except Exception:
        stale = policy["stale_while_revalidate_seconds"]

    if stale < 0:
        stale = 0
    if stale > 3600:
        stale = 3600

    policy["stale_while_revalidate_seconds"] = stale

    return policy


def validate_cache_policy(policy: Any) -> tuple[bool, list[str], dict[str, Any]]:
    normalized = normalize_cache_policy(policy)
    errors: list[str] = []

    if not isinstance(policy, dict):
        if policy is not None:
            errors.append("cache policy must be an object")
        return False if errors else True, errors, normalized

    unknown = sorted(set(policy.keys()) - CACHE_ALLOWED_KEYS)
    if unknown:
        errors.append(f"unsupported cache keys: {unknown}")

    if normalized["enabled"]:
        if normalized["ttl_seconds"] < 1:
            errors.append("enabled cache requires ttl_seconds >= 1")

        if not normalized["methods"]:
            errors.append("enabled cache requires at least one method")

        for method in normalized["methods"]:
            if method not in CACHE_ALLOWED_METHODS:
                errors.append(f"unsupported cache method: {method}")

        if "authorization" not in normalized["bypass_headers"]:
            errors.append("enabled cache must bypass authorization header")

        if "x-api-key" not in normalized["bypass_headers"]:
            errors.append("enabled cache must bypass x-api-key header")

    return not errors, errors, normalized


def normalize_route_cache(route: dict[str, Any]) -> dict[str, Any]:
    updated = dict(route)
    updated["cache"] = normalize_cache_policy(route.get("cache"))
    return updated


def normalize_config_cache_policies(config: dict[str, Any]) -> dict[str, Any]:
    updated = dict(config)
    routes = updated.get("routes") or []

    if not isinstance(routes, list):
        updated["routes"] = []
        return updated

    normalized_routes = []
    for route in routes:
        if isinstance(route, dict):
            normalized_routes.append(normalize_route_cache(route))
        else:
            normalized_routes.append(route)

    updated["routes"] = normalized_routes
    return updated


def validate_config_cache_policies(config: dict[str, Any]) -> tuple[bool, list[dict[str, Any]]]:
    failures: list[dict[str, Any]] = []

    routes = config.get("routes") or []
    if not isinstance(routes, list):
        return False, [{"path": None, "errors": ["routes must be a list"]}]

    for route in routes:
        if not isinstance(route, dict):
            continue

        ok, errors, normalized = validate_cache_policy(route.get("cache"))

        if not ok:
            failures.append(
                {
                    "path": route.get("path"),
                    "errors": errors,
                    "normalized": normalized,
                }
            )

    return not failures, failures
