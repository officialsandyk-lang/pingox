from __future__ import annotations

from typing import Any


DEFAULT_BLOCKED_PATHS = [
    "/.env",
    "/.git",
    "/.git/config",
    "/wp-admin",
    "/phpmyadmin",
    "/admin.php",
    "/test.php",
    "/backup.sql",
    "/config.bak",
]


def _as_list(value: Any, fallback: list[str]) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return fallback


def _security(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("security") or config.get("edge_security") or {}
    if isinstance(value, dict):
        return value
    return {}


def _max_body_size(config: dict[str, Any]) -> str:
    security = _security(config)

    raw = (
        security.get("max_request_body")
        or security.get("max_request_body_bytes")
        or config.get("max_request_body")
        or config.get("max_body_size")
    )

    if isinstance(raw, int) and raw > 0:
        return str(raw)

    if isinstance(raw, str) and raw.strip():
        return raw.strip()

    return "10m"


def _api_key_required(config: dict[str, Any]) -> bool:
    security = _security(config)
    return bool(
        security.get("api_key_required")
        or security.get("require_api_key")
        or config.get("api_key_required")
    )


def _api_key_value(config: dict[str, Any]) -> str:
    security = _security(config)
    value = (
        security.get("api_key_value")
        or security.get("api_key")
        or config.get("api_key_value")
        or config.get("api_key")
        or "CHANGE_ME"
    )
    return str(value)


def _blocked_paths(config: dict[str, Any]) -> list[str]:
    security = _security(config)
    return _as_list(
        security.get("blocked_paths")
        or security.get("blocked")
        or config.get("blocked_paths"),
        DEFAULT_BLOCKED_PATHS,
    )


def render_nginx_conf(
    *,
    active_port: int,
    server_name: str = "_",
    public_http_port: int = 80,
    config: dict[str, Any] | None = None,
) -> str:
    gateway_config = config or {}

    blocked_locations = "\n".join(
        f"""    location = {path} {{
        return 403;
    }}"""
        for path in _blocked_paths(gateway_config)
    )

    api_key_block = ""
    if _api_key_required(gateway_config):
        api_key = _api_key_value(gateway_config)
        api_key_block = f"""
    set $pingora_ai_api_key_ok 0;

    if ($http_x_api_key = "{api_key}") {{
        set $pingora_ai_api_key_ok 1;
    }}

    location /api/ {{
        if ($pingora_ai_api_key_ok = 0) {{
            return 401;
        }}

        proxy_pass http://pingora_ai_active;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
"""

    return f"""# Managed by pingora-ai bare_metal_linux provider.
# Do not edit manually.

upstream pingora_ai_active {{
    server 127.0.0.1:{active_port};
    keepalive 32;
}}

server {{
    listen {public_http_port} default_server;
    server_name {server_name};

    client_max_body_size {_max_body_size(gateway_config)};

    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;
    add_header Referrer-Policy "no-referrer" always;
    add_header Permissions-Policy "geolocation=(), microphone=(), camera=()" always;
    add_header X-Permitted-Cross-Domain-Policies "none" always;

{blocked_locations}

{api_key_block}

    location / {{
        proxy_pass http://pingora_ai_active;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
}}
"""


def render_nginx_conf_for_color(
    *,
    color: str,
    server_name: str = "_",
    public_http_port: int = 80,
    config: dict[str, Any] | None = None,
) -> str:
    from core.bare_metal.state import color_port

    return render_nginx_conf(
        active_port=color_port(color),
        server_name=server_name,
        public_http_port=public_http_port,
        config=config,
    )
