from __future__ import annotations

import copy
from typing import Any


PRODUCTION_ENVIRONMENTS = {
    "production",
    "prod",
    "kubernetes",
    "k8s",
    "ecs",
    "vm",
    "bare_metal",
    "baremetal",
}


def normalize_environment(value: str | None) -> str:
    text = str(value or "docker_local").strip().lower().replace("-", "_")

    aliases = {
        "prod": "production",
        "k8s": "kubernetes",
        "baremetal": "bare_metal",
        "docker": "docker_local",
        "docker_host": "docker_local",
        "local": "local",
    }

    return aliases.get(text, text)


def production_required(environment: str | None) -> bool:
    return normalize_environment(environment) in PRODUCTION_ENVIRONMENTS


def apply_production_security_contract(
    config: dict[str, Any],
    *,
    environment: str = "docker_local",
) -> dict[str, Any]:
    fixed = copy.deepcopy(config or {})
    env = normalize_environment(environment)
    strict = production_required(env)

    security = fixed.setdefault("production_security", {})

    security.setdefault(
        "waf_waap",
        {
            "enabled": False,
            "required_in_production": True,
            "provider": None,
            "mode": "contract_only_until_real_edge",
            "checks": [
                "owasp_top_10_rules",
                "bot_detection",
                "api_abuse_detection",
                "managed_rules",
            ],
        },
    )

    security.setdefault(
        "https_tls",
        {
            "enabled": False,
            "required_in_production": True,
            "certificate_source": None,
            "redirect_http_to_https": False,
            "min_tls_version": "1.2",
            "hsts": False,
            "local_optional": True,
        },
    )

    security.setdefault(
        "ddos_rate_abuse",
        {
            "enabled": False,
            "required_in_production": True,
            "edge_rate_limits": True,
            "per_ip_rate_limits": True,
            "bot_rate_limits": True,
            "burst_control": True,
            "provider": None,
        },
    )

    security.setdefault(
        "authn_authz",
        {
            "enabled": False,
            "required_in_production": True,
            "supported": ["jwt", "oauth2", "api_key"],
            "mode": "gateway_or_edge_or_backend",
            "jwt_required_for_private_routes": True,
            "api_key_required_for_machine_routes": True,
            "backend_object_level_authorization_required": True,
        },
    )

    security.setdefault(
        "mtls_private_networking",
        {
            "enabled": False,
            "required_in_production": True,
            "mode": "mtls_or_private_network",
            "upstreams_private_only": True,
            "public_upstream_access_forbidden": True,
        },
    )

    security.setdefault(
        "secrets_management",
        {
            "enabled": False,
            "required_in_production": True,
            "provider": None,
            "no_plaintext_secrets_in_repo": True,
            "env_file_for_local_only": True,
            "rotation_policy_required": True,
        },
    )

    security.setdefault(
        "least_privilege",
        {
            "enabled": False,
            "required_in_production": True,
            "non_root_container_user": True,
            "drop_linux_capabilities": True,
            "read_only_root_filesystem": True,
            "no_new_privileges": True,
            "resource_limits_required": True,
        },
    )

    security.setdefault(
        "vulnerability_scanning",
        {
            "enabled": False,
            "required_in_production": True,
            "dependency_scan_required": True,
            "container_scan_required": True,
            "secret_scan_required": True,
            "last_scan_passed": False,
            "scan_provider": None,
        },
    )

    security.setdefault(
        "observability_slo",
        {
            "enabled": False,
            "required_in_production": True,
            "structured_logs": True,
            "metrics": True,
            "traces": True,
            "request_id_correlation": True,
            "dashboard_required": True,
            "alerts_required": True,
            "slo_required": True,
            "error_budget_required": True,
        },
    )

    security.setdefault(
        "audit_retention",
        {
            "enabled": True,
            "required_in_production": True,
            "mutation_audit_logs": True,
            "retention_days": 90,
            "tamper_resistant_storage_required_in_production": True,
        },
    )

    fixed.setdefault("container_security", {})
    fixed["container_security"].setdefault(
        "docker",
        {
            "non_root_user": strict,
            "read_only_root_filesystem": strict,
            "drop_capabilities": strict,
            "no_new_privileges": strict,
            "resource_limits": strict,
            "healthcheck_required": True,
            "restart_policy_required": True,
        },
    )

    fixed["production_security"]["environment"] = env
    fixed["production_security"]["strict_production_enforcement"] = strict

    return fixed


def validate_production_security_contract(
    config: dict[str, Any],
    *,
    environment: str = "docker_local",
) -> dict[str, Any]:
    env = normalize_environment(environment)
    strict = production_required(env)

    config = apply_production_security_contract(config, environment=env)
    security = config.get("production_security") or {}

    checks: list[dict[str, Any]] = []

    required_sections = [
        "waf_waap",
        "https_tls",
        "ddos_rate_abuse",
        "authn_authz",
        "mtls_private_networking",
        "secrets_management",
        "least_privilege",
        "vulnerability_scanning",
        "observability_slo",
        "audit_retention",
    ]

    for section in required_sections:
        value = security.get(section)

        exists = isinstance(value, dict)
        enabled = bool(value.get("enabled")) if exists else False
        required = bool(value.get("required_in_production")) if exists else True

        ok = exists and (enabled or not strict or not required)

        checks.append(
            {
                "name": section,
                "ok": ok,
                "exists": exists,
                "enabled": enabled,
                "required_in_production": required,
                "strict_environment": strict,
                "mode": value.get("mode") if isinstance(value, dict) else None,
            }
        )

    https = security.get("https_tls") or {}

    if strict:
        checks.append(
            {
                "name": "https_redirect_required",
                "ok": bool(https.get("enabled")) and bool(https.get("redirect_http_to_https")),
                "enabled": bool(https.get("enabled")),
                "redirect_http_to_https": bool(https.get("redirect_http_to_https")),
            }
        )

    vuln = security.get("vulnerability_scanning") or {}

    if strict:
        checks.append(
            {
                "name": "vulnerability_scans_passed",
                "ok": bool(vuln.get("enabled")) and bool(vuln.get("last_scan_passed")),
                "enabled": bool(vuln.get("enabled")),
                "last_scan_passed": bool(vuln.get("last_scan_passed")),
            }
        )

    container_security = (config.get("container_security") or {}).get("docker") or {}

    checks.append(
        {
            "name": "container_security_contract_present",
            "ok": isinstance(container_security, dict) and bool(container_security),
            "container_security": container_security,
        }
    )

    failures = [check for check in checks if not check.get("ok")]

    return {
        "success": not failures,
        "environment": env,
        "strict_production_enforcement": strict,
        "checks": checks,
        "failures": failures,
    }
