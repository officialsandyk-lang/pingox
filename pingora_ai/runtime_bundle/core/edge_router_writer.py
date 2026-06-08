from __future__ import annotations

from pathlib import Path
import os
from typing import Any

from core.deployment_state import COLORS, PROJECT_STORE_DIR

EDGE_ROUTER_DIR = PROJECT_STORE_DIR / "edge-router"
EDGE_NETWORK = "pingora-edge-net"
EDGE_CONTAINER_NAME = "pingora-edge-router"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def edge_api_key_for_nginx() -> str:
    env_key = os.getenv("PINGORA_EDGE_API_KEY", "").strip()
    if env_key:
        return env_key

    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, raw_value = line.split("=", 1)
            if key.strip() == "PINGORA_EDGE_API_KEY":
                value = raw_value.strip().strip('"').strip("'")
                if value:
                    return value

    return "dev-local-secret"


def public_port_from_config(config: dict[str, Any]) -> int:
    """
    Get the user-defined public port.

    Supported config keys:
      - port
      - listen_port
      - proxy_port
      - public_port

    Example:
      {"port": 8088} -> edge router binds host port 8088
    """
    for key in ("port", "listen_port", "proxy_port", "public_port"):
        value = config.get(key)

        if value is None or value == "":
            continue

        try:
            port = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid port value for '{key}': {value!r}")

        if port < 1 or port > 65535:
            raise ValueError(f"Port must be between 1 and 65535, got: {port}")

        return port

    # Default only if user did not provide a port.
    return 9000


def internal_port_from_config(config: dict[str, Any]) -> int:
    """
    Get the port used inside the blue/green Pingora containers.

    By default, the generated Pingora app listens on the same port as the
    user-defined public port.

    Optional override keys:
      - internal_port
      - container_port
      - service_port
    """
    for key in ("internal_port", "container_port", "service_port"):
        value = config.get(key)

        if value is None or value == "":
            continue

        try:
            port = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid internal port value for '{key}': {value!r}")

        if port < 1 or port > 65535:
            raise ValueError(f"Internal port must be between 1 and 65535, got: {port}")

        return port

    return public_port_from_config(config)


def public_bind_host_from_config(config: dict[str, Any]) -> str:
    """
    Host interface for the public edge router port.

    Default:
      0.0.0.0 means accessible from host/network depending on Docker/WSL setup.

    Optional config keys:
      - bind_host
      - host
      - public_host

    For local-only binding, config can use:
      {"bind_host": "127.0.0.1"}
    """
    value = (
        config.get("bind_host")
        or config.get("host")
        or config.get("public_host")
        or "0.0.0.0"
    )

    return str(value).strip()


def live_url_from_config(config: dict[str, Any]) -> str:
    """
    User-facing live URL based on the user-defined public port.
    """
    public_port = public_port_from_config(config)

    # Use localhost for user display, even if Docker binds 0.0.0.0.
    return f"http://127.0.0.1:{public_port}"


def write_edge_router_files(active_color: str, config: dict[str, Any]) -> Path:
    """
    Write NGINX edge router files with TLS, HTTPS redirect, API-key auth,
    blocked paths, secure headers, body limits, and rate-limit config.
    """
    if active_color not in COLORS:
        raise ValueError(f"Invalid active color: {active_color}")

    EDGE_ROUTER_DIR.mkdir(parents=True, exist_ok=True)

    public_port = public_port_from_config(config)
    internal_port = internal_port_from_config(config)
    bind_host = public_bind_host_from_config(config)

    https_port = int(config.get("https_port") or os.getenv("PINGORA_TLS_HTTPS_PORT", "8443"))
    expected_api_key = edge_api_key_for_nginx()

    upstream_host = f"pingora-{active_color}"

    nginx_conf = f"""limit_req_zone $binary_remote_addr zone=ai_pingora_edge_rate:10m rate=1000r/s;

upstream active_pingora {{
    server {upstream_host}:{internal_port} max_fails=3 fail_timeout=10s;
    keepalive 64;
}}

server {{
    listen 80;
    server_name _;

    location = /__edge_health {{
        add_header Content-Type text/plain;
        return 200 "ok\\n";
    }}

    location / {{
        return 308 https://$host:{https_port}$request_uri;
    }}
}}

server {{
    listen 443 ssl;
    server_name _;

    ssl_certificate /etc/nginx/tls/local.crt;
    ssl_certificate_key /etc/nginx/tls/local.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers on;

    server_tokens off;

    client_max_body_size 10m;
    client_body_timeout 15s;
    client_header_timeout 15s;
    send_timeout 30s;

    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;
    add_header Referrer-Policy "no-referrer" always;
    add_header Permissions-Policy "camera=(), microphone=(), geolocation=()" always;
    add_header X-Permitted-Cross-Domain-Policies "none" always;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    limit_req zone=ai_pingora_edge_rate burst=2000 nodelay;

    location = /__edge_health {{
        add_header Content-Type text/plain;
        return 200 "ok\\n";
    }}

    set $api_key_valid 0;
    if ($http_x_api_key = "{expected_api_key}") {{
        set $api_key_valid 1;
    }}

    if ($api_key_valid = 0) {{
        return 401;
    }}

    location ~* ^/(\\.env|\\.git|\\.git/|private|internal|debug|wp-admin|wp-login\\.php|phpmyadmin|admin\\.php) {{
        return 403;
    }}

    location ~* \\.(php|phtml|sql|bak|old|swp|ini|env|log)$ {{
        return 403;
    }}

    location / {{
        proxy_http_version 1.1;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;

        proxy_connect_timeout 5s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;

        proxy_pass http://active_pingora;
    }}
}}
"""

    compose_yml = f"""services:
  edge-router:
    image: nginx:1.27-alpine
    container_name: {EDGE_CONTAINER_NAME}
    restart: unless-stopped
    ports:
      - "{bind_host}:{public_port}:80"
      - "{bind_host}:{https_port}:443"
    volumes:
      - ./default.conf:/etc/nginx/conf.d/default.conf:ro
      - ../../data/tls:/etc/nginx/tls:ro
    networks:
      - {EDGE_NETWORK}

networks:
  {EDGE_NETWORK}:
    external: true
"""

    nginx_conf_path = EDGE_ROUTER_DIR / "default.conf"
    compose_path = EDGE_ROUTER_DIR / "docker-compose.yml"

    nginx_conf_path.write_text(nginx_conf, encoding="utf-8")
    compose_path.write_text(compose_yml, encoding="utf-8")

    return compose_path

