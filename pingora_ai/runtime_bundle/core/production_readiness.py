from __future__ import annotations

import copy
import json
import sqlite3
from datetime import datetime, timezone
UTC = timezone.utc
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "reliability_reports.sqlite3"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)

    for key, value in overlay.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result.setdefault(key, copy.deepcopy(value))

    return result


def _check(name: str, ok: bool, **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "ok": bool(ok),
        **extra,
    }


def production_readiness_profile(environment: str = "local") -> dict[str, Any]:
    local = environment == "local"

    return {
        "edge_protection": {
            "waf_waap": {
                "enabled": True,
                "local_mode": "policy_contract_only",
                "production_required": True,
                "expected_provider": "cloudflare_aws_azure_nginx_or_equivalent",
                "protects_against": [
                    "sql_injection",
                    "xss",
                    "path_traversal",
                    "known_bad_bots",
                    "suspicious_payloads",
                    "protocol_abuse",
                ],
            },
            "ddos_protection": {
                "enabled": True,
                "local_mode": "policy_contract_only",
                "production_required": True,
                "layers": ["network", "transport", "application"],
            },
            "https_tls": {
                "enabled": not local,
                "local_optional": True,
                "production_required": True,
                "min_tls_version": "TLS1.2",
                "preferred_tls_version": "TLS1.3",
                "redirect_http_to_https": not local,
            },
            "secure_headers": {
                "enabled": True,
                "headers": {
                    "X-Content-Type-Options": "nosniff",
                    "X-Frame-Options": "DENY",
                    "Referrer-Policy": "no-referrer",
                    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
                    "Content-Security-Policy": "default-src 'self'",
                },
            },
            "cors_policy": {
                "enabled": True,
                "local_allowed_origins": ["http://127.0.0.1", "http://localhost"],
                "production_allowed_origins_required": True,
                "allowed_methods": ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
                "allowed_headers": ["Content-Type", "Authorization", "Idempotency-Key", "X-Request-ID"],
            },
            "bot_rate_abuse_controls": {
                "enabled": True,
                "local_mode": "rate_limit_contract",
                "ip_rate_limit_per_minute": 10000 if local else 1200,
                "token_rate_limit_per_minute": 10000 if local else 600,
                "burst_limit": 200,
            },
        },
        "gateway_runtime_protection": {
            "active_health_checks": {
                "enabled": True,
                "interval_seconds": 10,
                "timeout_seconds": 3,
                "failure_threshold": 3,
            },
            "passive_health_checks": {
                "enabled": True,
                "failure_statuses": [500, 502, 503, 504],
                "failure_threshold": 5,
            },
            "readiness_check": {
                "enabled": True,
                "path": "/",
                "expected_status": 200,
            },
            "liveness_check": {
                "enabled": True,
                "path": "/",
                "expected_status": 200,
            },
            "upstream_timeout_policy": {
                "enabled": True,
                "seconds": 30,
            },
            "retry_policy": {
                "enabled": True,
                "max_retries": 2,
                "retry_on": [502, 503, 504],
                "do_not_retry_methods": ["POST", "PATCH", "DELETE"],
            },
            "circuit_breaker_policy": {
                "enabled": True,
                "failure_threshold": 5,
                "cooldown_seconds": 30,
                "half_open_probe_count": 2,
            },
            "schema_validation": {
                "enabled": True,
                "local_mode": "contract_only",
                "production_mode": "gateway_or_backend_json_schema",
                "content_type": "application/json",
            },
            "mutation_audit": {
                "enabled": True,
                "methods": ["POST", "PUT", "PATCH", "DELETE"],
                "log_path": "data/audit/crud_mutations.jsonl",
            },
        },
        "deployment_safety": {
            "pre_change_backup": {
                "enabled": True,
                "path": "data/update_backups",
                "required_before": ["main_overwrite", "route_add", "route_delete", "route_edit", "security_change", "healing_repair"],
            },
            "deployment_strategy": {
                "local": "direct_with_backup_and_verification",
                "production": "bluegreen_or_canary_required",
            },
            "automatic_rollback": {
                "enabled": True,
                "trigger_on_verification_failure": True,
                "verify_after_rollback": True,
            },
            "post_change_full_verification": {
                "enabled": True,
                "level": "full",
            },
            "prompt_intent_contract": {
                "enabled": True,
                "required_before_deploy": True,
            },
        },
        "observability": {
            "structured_logs": {
                "enabled": True,
                "format": "jsonl",
                "path": "logs.jsonl",
            },
            "metrics": {
                "enabled": True,
                "local_mode": "sqlite_and_logs",
                "production_mode": "prometheus_or_equivalent",
            },
            "traces": {
                "enabled": True,
                "local_mode": "optional_langsmith",
                "production_mode": "opentelemetry_or_equivalent",
            },
            "request_correlation": {
                "enabled": True,
                "header": "X-Request-ID",
            },
            "dashboard": {
                "enabled": True,
                "local_mode": "route_directory_and_sqlite_reports",
                "production_mode": "grafana_or_equivalent",
            },
            "alerts": {
                "enabled": True,
                "local_mode": "console_and_sqlite",
                "production_mode": "email_slack_pagerduty_or_equivalent",
            },
            "slo_error_budget": {
                "enabled": True,
                "availability_target_percent": 99.9,
                "latency_target_ms": 500,
                "error_rate_budget_percent": 0.1,
            },
        },
        "self_healing_intelligence": {
            "log_based_detection": True,
            "verifier_based_detection": True,
            "root_cause_classification": True,
            "deterministic_repair_actions": True,
            "restart_regenerate_redeploy_actions": True,
            "rollback_when_repair_fails": True,
            "sqlite_learning_store": True,
            "mttr_reporting": True,
            "repeated_incident_classification": True,
        },
        "security_and_data_protection": {
            "auth_policy": {
                "enabled": True,
                "supported": ["JWT", "OAuth2", "API-Key"],
                "local_mode": "contract_or_header_presence",
                "production_required": True,
            },
            "backend_object_level_authorization": {
                "required": True,
                "owner": "backend_service",
                "gateway_role": "forward_identity_and_enforce_route_policy",
            },
            "mtls_or_private_networking": {
                "enabled": True,
                "local_mode": "contract_only",
                "production_required": True,
            },
            "secrets_management": {
                "enabled": True,
                "local": ".env_not_committed",
                "production": "secret_manager_required",
            },
            "least_privilege": {
                "enabled": True,
                "applies_to": ["runtime", "deployment", "secrets", "logs"],
            },
            "audit_log_retention": {
                "enabled": True,
                "local_days": 7,
                "production_days": 90,
            },
            "vulnerability_scanning": {
                "enabled": True,
                "local": "cargo_audit_or_dependency_review",
                "production": "ci_security_scan_required",
            },
            "dependency_scanning": {
                "enabled": True,
                "tools": ["cargo audit", "pip-audit", "github_dependabot_or_equivalent"],
            },
        },
    }


def apply_production_readiness_profile(
    config: dict[str, Any],
    *,
    environment: str = "local",
) -> dict[str, Any]:
    fixed = copy.deepcopy(config)

    fixed.setdefault("production_readiness", {})
    fixed["production_readiness"] = _deep_merge(
        fixed["production_readiness"],
        production_readiness_profile(environment),
    )

    security = fixed.setdefault("security", {})

    if not isinstance(security, dict):
        security = {}
        fixed["security"] = security

    edge = fixed["production_readiness"]["edge_protection"]
    runtime = fixed["production_readiness"]["gateway_runtime_protection"]

    security.setdefault("secure_headers", edge["secure_headers"])
    security.setdefault("cors_policy", edge["cors_policy"])
    security.setdefault("bot_rate_abuse_controls", edge["bot_rate_abuse_controls"])
    security.setdefault("auth_policy", fixed["production_readiness"]["security_and_data_protection"]["auth_policy"])
    security.setdefault("schema_validation", runtime["schema_validation"])
    security.setdefault("retry_policy", runtime["retry_policy"])
    security.setdefault("circuit_breaker_policy", runtime["circuit_breaker_policy"])
    security.setdefault("active_health_checks", runtime["active_health_checks"])
    security.setdefault("passive_health_checks", runtime["passive_health_checks"])

    security.setdefault("max_request_body_bytes", 10_485_760)
    security.setdefault("upstream_timeout_seconds", 30)

    routes = fixed.get("routes") or []

    if isinstance(routes, list):
        for route in routes:
            if not isinstance(route, dict):
                continue

            route_type = str(route.get("type") or "").lower()

            if route_type in {"static", "web", "webserver", "web_server", "file_server", "files"}:
                continue

            route.setdefault("health_check", {
                "enabled": True,
                "path": route.get("path", "/"),
                "interval_seconds": 10,
                "timeout_seconds": 3,
                "failure_threshold": 3,
            })

            route.setdefault("retry_policy", runtime["retry_policy"])
            route.setdefault("circuit_breaker_policy", runtime["circuit_breaker_policy"])
            route.setdefault("schema_validation", runtime["schema_validation"])

            route.setdefault("allowed_methods", ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])

            route.setdefault("crud_policy", {
                "enabled": True,
                "allow_methods": ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
                "mutation_methods": ["POST", "PUT", "PATCH", "DELETE"],
                "require_auth_in_production": True,
                "require_json_content_type": True,
                "require_idempotency_key_for_mutations": True,
                "audit_mutations": True,
                "staging_safe_probe_path": f"{str(route.get('path') or '').rstrip('/')}/__gateway_crud_probe__/resource/123",
            })

    return fixed


def validate_production_readiness(
    config: dict[str, Any],
    *,
    environment: str = "local",
    store: bool = True,
    source: str = "manual",
) -> dict[str, Any]:
    readiness = config.get("production_readiness") or {}
    security = config.get("security") or {}

    checks: list[dict[str, Any]] = []

    edge = readiness.get("edge_protection") or {}
    runtime = readiness.get("gateway_runtime_protection") or {}
    deploy = readiness.get("deployment_safety") or {}
    obs = readiness.get("observability") or {}
    heal = readiness.get("self_healing_intelligence") or {}
    data = readiness.get("security_and_data_protection") or {}

    checks.extend([
        _check("WAF/WAAP policy configured", bool((edge.get("waf_waap") or {}).get("enabled")), value=edge.get("waf_waap")),
        _check("DDoS protection policy configured", bool((edge.get("ddos_protection") or {}).get("enabled")), value=edge.get("ddos_protection")),
        _check("HTTPS/TLS policy configured", bool((edge.get("https_tls") or {}).get("production_required")), value=edge.get("https_tls")),
        _check("secure headers configured", bool((edge.get("secure_headers") or {}).get("enabled")), value=edge.get("secure_headers")),
        _check("CORS policy configured", bool((edge.get("cors_policy") or {}).get("enabled")), value=edge.get("cors_policy")),
        _check("bot/rate-abuse controls configured", bool((edge.get("bot_rate_abuse_controls") or {}).get("enabled")), value=edge.get("bot_rate_abuse_controls")),
    ])

    checks.extend([
        _check("active health checks configured", bool((runtime.get("active_health_checks") or {}).get("enabled")), value=runtime.get("active_health_checks")),
        _check("passive health checks configured", bool((runtime.get("passive_health_checks") or {}).get("enabled")), value=runtime.get("passive_health_checks")),
        _check("readiness check configured", bool((runtime.get("readiness_check") or {}).get("enabled")), value=runtime.get("readiness_check")),
        _check("liveness check configured", bool((runtime.get("liveness_check") or {}).get("enabled")), value=runtime.get("liveness_check")),
        _check("upstream timeout configured", bool((runtime.get("upstream_timeout_policy") or {}).get("enabled")), value=runtime.get("upstream_timeout_policy")),
        _check("retry policy configured", bool((runtime.get("retry_policy") or {}).get("enabled")), value=runtime.get("retry_policy")),
        _check("circuit breaker policy configured", bool((runtime.get("circuit_breaker_policy") or {}).get("enabled")), value=runtime.get("circuit_breaker_policy")),
        _check("request body limit configured", int(security.get("max_request_body_bytes") or 0) > 0, value=security.get("max_request_body_bytes")),
        _check("schema/content-type validation policy configured", bool((runtime.get("schema_validation") or {}).get("enabled")), value=runtime.get("schema_validation")),
        _check("mutation audit policy configured", bool((runtime.get("mutation_audit") or {}).get("enabled")), value=runtime.get("mutation_audit")),
    ])

    checks.extend([
        _check("pre-change backup policy configured", bool((deploy.get("pre_change_backup") or {}).get("enabled")), value=deploy.get("pre_change_backup")),
        _check("blue/green or canary required for production", "bluegreen" in json.dumps(deploy).lower() or "canary" in json.dumps(deploy).lower(), value=deploy.get("deployment_strategy")),
        _check("automatic rollback policy configured", bool((deploy.get("automatic_rollback") or {}).get("enabled")), value=deploy.get("automatic_rollback")),
        _check("post-change full verification configured", bool((deploy.get("post_change_full_verification") or {}).get("enabled")), value=deploy.get("post_change_full_verification")),
        _check("prompt intent contract required", bool((deploy.get("prompt_intent_contract") or {}).get("enabled")), value=deploy.get("prompt_intent_contract")),
    ])

    checks.extend([
        _check("structured logs configured", bool((obs.get("structured_logs") or {}).get("enabled")), value=obs.get("structured_logs")),
        _check("metrics configured", bool((obs.get("metrics") or {}).get("enabled")), value=obs.get("metrics")),
        _check("traces configured", bool((obs.get("traces") or {}).get("enabled")), value=obs.get("traces")),
        _check("request correlation configured", bool((obs.get("request_correlation") or {}).get("enabled")), value=obs.get("request_correlation")),
        _check("dashboard configured", bool((obs.get("dashboard") or {}).get("enabled")), value=obs.get("dashboard")),
        _check("alerts configured", bool((obs.get("alerts") or {}).get("enabled")), value=obs.get("alerts")),
        _check("SLO/error budget configured", bool((obs.get("slo_error_budget") or {}).get("enabled")), value=obs.get("slo_error_budget")),
    ])

    for key in [
        "log_based_detection",
        "verifier_based_detection",
        "root_cause_classification",
        "deterministic_repair_actions",
        "restart_regenerate_redeploy_actions",
        "rollback_when_repair_fails",
        "sqlite_learning_store",
        "mttr_reporting",
        "repeated_incident_classification",
    ]:
        checks.append(_check(f"self-healing {key}", bool(heal.get(key)), value=heal.get(key)))

    checks.extend([
        _check("JWT/OAuth/API-key validation policy configured", bool((data.get("auth_policy") or {}).get("enabled")), value=data.get("auth_policy")),
        _check("backend object-level authorization contract configured", bool((data.get("backend_object_level_authorization") or {}).get("required")), value=data.get("backend_object_level_authorization")),
        _check("mTLS/private networking contract configured", bool((data.get("mtls_or_private_networking") or {}).get("production_required")), value=data.get("mtls_or_private_networking")),
        _check("secrets management configured", bool((data.get("secrets_management") or {}).get("enabled")), value=data.get("secrets_management")),
        _check("least privilege configured", bool((data.get("least_privilege") or {}).get("enabled")), value=data.get("least_privilege")),
        _check("audit log retention configured", bool((data.get("audit_log_retention") or {}).get("enabled")), value=data.get("audit_log_retention")),
        _check("vulnerability scanning configured", bool((data.get("vulnerability_scanning") or {}).get("enabled")), value=data.get("vulnerability_scanning")),
        _check("dependency scanning configured", bool((data.get("dependency_scanning") or {}).get("enabled")), value=data.get("dependency_scanning")),
    ])

    required_files = [
        "main.py",
        "rollback.py",
        "heal.py",
        "core/log_healing.py",
        "core/post_change_verifier.py",
        "core/intent_contract.py",
        "core/deployment_prepare.py",
        "agents/root_cause_agent.py",
    ]

    for file_name in required_files:
        path = PROJECT_ROOT / file_name
        checks.append(_check(f"required file exists: {file_name}", path.exists(), path=str(path)))

    routes = config.get("routes") or []
    routes_missing_policy = []

    if isinstance(routes, list):
        for route in routes:
            if not isinstance(route, dict):
                continue

            route_type = str(route.get("type") or "").lower()

            if route_type in {"static", "web", "webserver", "web_server", "file_server", "files"}:
                continue

            if not route.get("health_check") or not route.get("circuit_breaker_policy") or not route.get("crud_policy"):
                routes_missing_policy.append(route.get("path"))

    checks.append(_check(
        "per-route health/check/circuit/crud policies configured",
        not routes_missing_policy,
        routes_missing_policy=routes_missing_policy,
    ))

    failed = [check for check in checks if not check["ok"]]

    result = {
        "success": not failed,
        "created_at": utc_now(),
        "source": source,
        "environment": environment,
        "check_count": len(checks),
        "failure_count": len(failed),
        "checks": checks,
        "failed_checks": failed,
    }

    if store:
        store_production_readiness_result(result)

    return result


def store_production_readiness_result(
    result: dict[str, Any],
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> None:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS production_readiness_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                source TEXT,
                environment TEXT,
                success INTEGER NOT NULL,
                check_count INTEGER,
                failure_count INTEGER,
                result_json TEXT
            )
            """
        )

        conn.execute(
            """
            INSERT INTO production_readiness_runs (
                created_at,
                source,
                environment,
                success,
                check_count,
                failure_count,
                result_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.get("created_at") or utc_now(),
                result.get("source"),
                result.get("environment"),
                1 if result.get("success") else 0,
                result.get("check_count"),
                result.get("failure_count"),
                json.dumps(result, indent=2, default=str),
            ),
        )

        conn.commit()


def format_production_readiness_report(result: dict[str, Any]) -> str:
    lines = [
        "🛡️ Local production-readiness check",
        "",
        f"Success: {result.get('success')}",
        f"Environment: {result.get('environment')}",
        f"Checks: {result.get('check_count')}",
        f"Failures: {result.get('failure_count')}",
    ]

    failed = result.get("failed_checks") or []

    if failed:
        lines.append("")
        lines.append("Failed checks:")

        for check in failed:
            lines.append(f"- {check.get('name')} | {check}")

    else:
        lines.append("")
        lines.append("All production-readiness checks passed ✅")

    return "\n".join(lines)
