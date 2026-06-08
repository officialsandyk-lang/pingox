from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


CONTRACT_VERSION = "2026-05-security-contract-v1"

DEFAULT_ALLOWED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]

DEFAULT_BLOCKED_PATHS = [
    "/.env",
    "/.git",
    "/.git/config",
    "/private",
    "/internal",
    "/debug",
    "/admin.php",
    "/wp-admin",
    "/wp-login.php",
    "/phpmyadmin",
]

DEFAULT_BLOCKED_EXTENSIONS = [
    ".php",
    ".phtml",
    ".bak",
    ".old",
    ".swp",
    ".sql",
    ".ini",
    ".env",
]

DEFAULT_SUSPICIOUS_PATTERNS = [
    "../",
    "..%2f",
    "%2e%2e",
    "<script",
    "union select",
    "information_schema",
    "/etc/passwd",
    "cmd=",
    "exec=",
    "powershell",
]

DEFAULT_METHOD_RATE_LIMITS = {
    "GET": 6000,
    "HEAD": 6000,
    "OPTIONS": 6000,
    "POST": 1200,
    "PUT": 900,
    "PATCH": 900,
    "DELETE": 300,
}


def _empty(value: Any) -> bool:
    return value is None or value == [] or value == {}


def _force(container: dict[str, Any], key: str, value: Any) -> None:
    if _empty(container.get(key)):
        container[key] = copy.deepcopy(value)


def _ensure_dict(container: dict[str, Any], key: str) -> dict[str, Any]:
    value = container.get(key)
    if not isinstance(value, dict):
        value = {}
        container[key] = value
    return value


def _environment_name(environment: str | None) -> str:
    text = str(environment or "local").strip().lower()
    if text in {"docker", "docker_host", "docker-local", "docker_local"}:
        return "docker_host"
    if text in {"prod", "production", "strict"}:
        return "production"
    return "local"


def default_https_contract(environment: str = "local") -> dict[str, Any]:
    env = _environment_name(environment)
    return {
        "enabled": False,
        "local_optional": env != "production",
        "production_required": True,
        "termination": "edge_or_gateway",
        "redirect_http_to_https": False,
        "certificate_source": "external_or_future_gateway_tls",
        "minimum_tls_version": "TLSv1.2",
        "hsts": {
            "enabled": env == "production",
            "max_age_seconds": 31536000,
            "include_subdomains": True,
            "preload": False,
        },
        "local_enforcement_mode": "verify_config",
        "production_enforcement_mode": "gateway_or_edge",
    }


def default_waf_contract(environment: str = "local") -> dict[str, Any]:
    env = _environment_name(environment)
    return {
        "enabled": True,
        "mode": "detect" if env != "production" else "enforce",
        "profile": "developer_preview_baseline",
        "blocked_paths": DEFAULT_BLOCKED_PATHS,
        "blocked_extensions": DEFAULT_BLOCKED_EXTENSIONS,
        "suspicious_patterns": DEFAULT_SUSPICIOUS_PATTERNS,
        "max_query_length": 2048,
        "max_header_size_bytes": 16384,
        "max_body_bytes": 1048576,
        "block_status_code": 403,
        "owasp_crs_compatible_contract": True,
        "local_enforcement_mode": "verify_config",
        "production_enforcement_mode": "gateway_or_edge",
    }


def default_secure_headers_contract(environment: str = "local") -> dict[str, Any]:
    return {
        "enabled": True,
        "headers": {
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
        },
        "hsts_only_when_https": True,
        "local_enforcement_mode": "verify_config",
        "production_enforcement_mode": "gateway_or_edge",
    }


def default_cors_contract(environment: str = "local") -> dict[str, Any]:
    return {
        "enabled": True,
        "allowed_origins": ["http://127.0.0.1:8090", "http://localhost:8090"],
        "allowed_methods": DEFAULT_ALLOWED_METHODS,
        "allowed_headers": [
            "Content-Type",
            "Authorization",
            "Idempotency-Key",
            "X-Gateway-CRUD-Probe",
            "X-Requested-With",
        ],
        "allow_credentials": False,
        "max_age_seconds": 600,
        "local_enforcement_mode": "verify_config",
        "production_enforcement_mode": "gateway_or_edge",
    }


def default_auth_contract(environment: str = "local") -> dict[str, Any]:
    env = _environment_name(environment)
    return {
        "enabled": False,
        "production_required": True,
        "accepted_schemes": ["jwt", "oauth2", "api_key"],
        "default_scheme": "jwt",
        "public_routes": ["/", "/health", "/__gateway_runtime_state"],
        "protected_routes": "all_proxy_routes_in_production",
        "jwt": {
            "issuer": None,
            "audience": None,
            "jwks_url": None,
            "algorithms": ["RS256", "ES256"],
        },
        "api_key": {
            "header": "X-API-Key",
            "secret_ref": "PINGORA_AI_API_KEY",
        },
        "local_enforcement_mode": "verify_config",
        "production_enforcement_mode": "gateway_or_edge",
        "strict_ready": env == "production",
    }


def default_secrets_contract(environment: str = "local") -> dict[str, Any]:
    env = _environment_name(environment)
    return {
        "enabled": True,
        "provider": "local_env" if env != "production" else "external_secret_manager",
        "never_commit_secrets": True,
        "required_secret_refs": [
            "OPENAI_API_KEY",
            "PINGORA_AI_API_TOKEN",
        ],
        "rotation_policy": {
            "enabled": True,
            "recommended_days": 90,
        },
        "secret_scanning_required": True,
        "local_enforcement_mode": "verify_config",
        "production_enforcement_mode": "external_secret_manager",
    }


def default_container_security_contract(environment: str = "local") -> dict[str, Any]:
    env = _environment_name(environment)
    return {
        "enabled": True,
        "non_root_user": env == "production",
        "read_only_root_filesystem": env == "production",
        "drop_capabilities": env == "production",
        "no_new_privileges": env == "production",
        "resource_limits_required": True,
        "network_policy_required": env == "production",
        "local_enforcement_mode": "verify_config",
        "production_enforcement_mode": "container_runtime_or_orchestrator",
    }


def default_scanning_contract(environment: str = "local") -> dict[str, Any]:
    return {
        "enabled": True,
        "dependency_scanning": {
            "enabled": True,
            "tools": ["pip-audit", "cargo-audit", "docker-scout"],
            "local_enforcement_mode": "manual_or_ci",
            "production_enforcement_mode": "ci_required",
        },
        "container_image_scanning": {
            "enabled": True,
            "tools": ["docker-scout", "trivy"],
            "local_enforcement_mode": "manual_or_ci",
            "production_enforcement_mode": "ci_required",
        },
        "secret_scanning": {
            "enabled": True,
            "tools": ["gitleaks", "github_secret_scanning"],
            "local_enforcement_mode": "manual_or_ci",
            "production_enforcement_mode": "ci_required",
        },
    }


def default_request_policy_contract(environment: str = "local") -> dict[str, Any]:
    return {
        "allowed_methods": DEFAULT_ALLOWED_METHODS,
        "blocked_paths": DEFAULT_BLOCKED_PATHS,
        "max_request_body_bytes": 1048576,
        "require_idempotency_key_for_mutations": True,
        "mutation_methods": ["POST", "PUT", "PATCH", "DELETE"],
        "allowed_content_types": ["application/json"],
        "method_rate_limits_per_minute": DEFAULT_METHOD_RATE_LIMITS,
        "local_enforcement_mode": "verify_config",
        "production_enforcement_mode": "gateway_or_edge",
    }


def _apply_route_policy(route: dict[str, Any], config: dict[str, Any]) -> None:
    if str(route.get("path") or "") == "/":
        return

    allowed_methods = config["security"]["request_policy"]["allowed_methods"]
    method_limits = config["security"]["request_policy"]["method_rate_limits_per_minute"]

    content_type_policy = {
        "enabled": True,
        "methods": ["POST", "PUT", "PATCH"],
        "allowed_content_types": ["application/json"],
        "local_enforcement_mode": "verify_config",
        "production_enforcement_mode": "gateway_or_backend",
    }

    idempotency_policy = {
        "enabled": True,
        "methods": ["POST", "PUT", "PATCH", "DELETE"],
        "required_header": "Idempotency-Key",
        "local_enforcement_mode": "verify_config",
        "production_enforcement_mode": "gateway_or_backend",
    }

    audit_policy = {
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
    }

    crud_probe_policy = {
        "enabled": True,
        "staging_safe_only": True,
        "probe_prefix": "__gateway_crud_probe__",
        "never_probe_real_destructive_business_endpoints": True,
    }

    route["allowed_methods"] = allowed_methods
    route["methods"] = allowed_methods
    route["method_allowlist"] = allowed_methods
    route["method_allow_list"] = allowed_methods
    route["method_rate_limits_per_minute"] = method_limits
    route["content_type_policy"] = content_type_policy
    route["idempotency_policy"] = idempotency_policy
    route["audit_policy"] = audit_policy
    route["crud_probe_policy"] = crud_probe_policy

    route["method_policy"] = {
        "enabled": True,
        "allowed_methods": allowed_methods,
        "methods": allowed_methods,
    }

    route["crud_policy"] = {
        "enabled": True,
        "allowed_methods": allowed_methods,
        "methods": allowed_methods,
        "content_type_policy": content_type_policy,
        "idempotency_policy": idempotency_policy,
        "audit_policy": audit_policy,
        "crud_probe_policy": crud_probe_policy,
    }

    route["crud"] = copy.deepcopy(route["crud_policy"])

    security = _ensure_dict(route, "security")
    policy = _ensure_dict(route, "policy")

    for container in (security, policy):
        container["allowed_methods"] = allowed_methods
        container["methods"] = allowed_methods
        container["method_allowlist"] = allowed_methods
        container["method_policy"] = copy.deepcopy(route["method_policy"])
        container["crud_policy"] = copy.deepcopy(route["crud_policy"])
        container["content_type_policy"] = content_type_policy
        container["idempotency_policy"] = idempotency_policy
        container["audit_policy"] = audit_policy
        container["crud_probe_policy"] = crud_probe_policy


def apply_security_policy_contract(
    config: dict[str, Any],
    environment: str = "local",
) -> dict[str, Any]:
    cfg = copy.deepcopy(config)
    env = _environment_name(environment)

    cfg["security_contract_version"] = CONTRACT_VERSION

    security = _ensure_dict(cfg, "security")
    security["contract_version"] = CONTRACT_VERSION
    security["environment"] = env

    _force(security, "https", default_https_contract(env))
    _force(security, "tls", security["https"])
    _force(security, "waf", default_waf_contract(env))
    _force(security, "secure_headers", default_secure_headers_contract(env))
    _force(security, "cors", default_cors_contract(env))
    _force(security, "auth", default_auth_contract(env))
    _force(security, "secrets", default_secrets_contract(env))
    _force(security, "container_security", default_container_security_contract(env))
    _force(security, "scanning", default_scanning_contract(env))
    # Always ensure request_policy exists. Some older local/healing paths create
    # a partial security object but omit security.request_policy.
    if not isinstance(security.get("request_policy"), dict) or not security.get("request_policy"):
        security["request_policy"] = default_request_policy_contract(env)

    # Backward-compatible top-level mirrors for existing verifier versions.
    cfg["https"] = security["https"]
    cfg["tls"] = security["tls"]
    cfg["waf"] = security["waf"]
    cfg["secure_headers"] = security["secure_headers"]
    cfg["cors"] = security["cors"]
    cfg["auth"] = security["auth"]
    cfg["secrets"] = security["secrets"]
    cfg["container_security"] = security["container_security"]
    cfg["scanning"] = security["scanning"]

    request_policy = security["request_policy"]

    cfg["allowed_methods"] = request_policy["allowed_methods"]
    cfg["blocked_paths"] = request_policy["blocked_paths"]
    cfg["max_request_body_bytes"] = request_policy["max_request_body_bytes"]
    cfg["method_rate_limits_per_minute"] = request_policy["method_rate_limits_per_minute"]

    cfg["content_type_policy"] = {
        "enabled": True,
        "methods": ["POST", "PUT", "PATCH"],
        "allowed_content_types": ["application/json"],
        "local_enforcement_mode": "verify_config",
        "production_enforcement_mode": "gateway_or_backend",
    }

    cfg["idempotency_policy"] = {
        "enabled": True,
        "methods": ["POST", "PUT", "PATCH", "DELETE"],
        "required_header": "Idempotency-Key",
        "local_enforcement_mode": "verify_config",
        "production_enforcement_mode": "gateway_or_backend",
    }

    cfg["audit_policy"] = {
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
    }

    cfg["crud_probe_policy"] = {
        "enabled": True,
        "staging_safe_only": True,
        "probe_prefix": "__gateway_crud_probe__",
        "never_probe_real_destructive_business_endpoints": True,
    }

    routes = cfg.get("routes")
    if not isinstance(routes, list):
        routes = []
        cfg["routes"] = routes

    for route in routes:
        if isinstance(route, dict):
            _apply_route_policy(route, cfg)

    return cfg


def _get(config: dict[str, Any], *paths: str) -> Any:
    for path in paths:
        current: Any = config
        ok = True

        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                ok = False
                break
            current = current[part]

        if ok:
            return current

    return None


def verify_security_policy_contract(
    config: dict[str, Any],
    strict: bool = False,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, **extra: Any) -> None:
        checks.append({"name": name, "ok": bool(ok), **extra})

    https = _get(config, "security.https", "https")
    waf = _get(config, "security.waf", "waf")
    auth = _get(config, "security.auth", "auth")
    secrets = _get(config, "security.secrets", "secrets")
    container = _get(config, "security.container_security", "container_security")
    scanning = _get(config, "security.scanning", "scanning")
    request_policy = _get(config, "security.request_policy")

    add("security contract version present", bool(config.get("security_contract_version")))
    add("TLS/SSL contract present", isinstance(https, dict) and bool(https))
    add("WAF/security contract present", isinstance(waf, dict) and bool(waf))
    add("auth contract present", isinstance(auth, dict) and bool(auth))
    add("secrets contract present", isinstance(secrets, dict) and bool(secrets))
    add("container hardening contract present", isinstance(container, dict) and bool(container))
    add("security scanning contract present", isinstance(scanning, dict) and bool(scanning))
    add("request policy contract present", isinstance(request_policy, dict) and bool(request_policy))

    blocked_paths = _get(config, "security.waf.blocked_paths", "security.request_policy.blocked_paths", "blocked_paths")
    add(
        "blocked paths configured",
        isinstance(blocked_paths, list)
        and set(DEFAULT_BLOCKED_PATHS).issubset(set(blocked_paths)),
        actual=blocked_paths or [],
    )

    suspicious = _get(config, "security.waf.suspicious_patterns", "waf.suspicious_patterns")
    add("WAF suspicious patterns configured", isinstance(suspicious, list) and len(suspicious) >= 3)

    methods = _get(config, "security.request_policy.allowed_methods", "allowed_methods")
    add(
        "allowed methods configured",
        isinstance(methods, list) and set(DEFAULT_ALLOWED_METHODS).issubset(set(methods)),
        actual=methods or [],
    )

    route_missing: list[str] = []

    for route in config.get("routes", []) or []:
        if not isinstance(route, dict):
            continue

        path = str(route.get("path") or "")

        if path == "/":
            continue

        has_methods = bool(
            route.get("allowed_methods")
            or route.get("methods")
            or route.get("method_allowlist")
            or route.get("method_policy")
            or _get(route, "policy.allowed_methods")
            or _get(route, "security.allowed_methods")
        )

        has_crud = bool(
            route.get("crud_policy")
            or route.get("crud")
            or _get(route, "policy.crud_policy")
            or _get(route, "security.crud_policy")
        )

        if not (has_methods and has_crud):
            route_missing.append(path)

    add(
        "per-route method allowlists and CRUD policies configured",
        not route_missing,
        routes_missing_policy=route_missing,
    )

    if strict:
        add("strict production TLS enabled", bool(https and https.get("enabled") is True))
        add("strict production HTTPS redirect enabled", bool(https and https.get("redirect_http_to_https") is True))
        add("strict production WAF enforce mode", bool(waf and waf.get("mode") in {"enforce", "blocking"}))
        add("strict production auth enabled", bool(auth and auth.get("enabled") is True))
        add(
            "strict production external secrets provider",
            bool(secrets and secrets.get("provider") not in {None, "", "local_env", "none"}),
        )
        add("strict production non-root container", bool(container and container.get("non_root_user") is True))
        add(
            "strict production read-only root filesystem",
            bool(container and container.get("read_only_root_filesystem") is True),
        )
        add("strict production drop capabilities", bool(container and container.get("drop_capabilities") is True))

    ok = all(item["ok"] for item in checks)

    return {
        "ok": ok,
        "success": ok,
        "strict": strict,
        "checks": checks,
        "failed_checks": [item for item in checks if not item["ok"]],
    }


def load_config(path: str | Path = "generated-pingora-proxy/config.json") -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_config(config: dict[str, Any], path: str | Path = "generated-pingora-proxy/config.json") -> None:
    Path(path).write_text(json.dumps(config, indent=2), encoding="utf-8")
