from __future__ import annotations


def render_pingora_service(
    *,
    service_name: str,
    binary_path: str,
    config_path: str,
    working_dir: str,
    listen_port: int,
    user: str = "www-data",
) -> str:
    return f"""[Unit]
Description=Pingora AI Gateway {service_name}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
Group={user}
WorkingDirectory={working_dir}
Environment=PINGORA_AI_CONFIG={config_path}
Environment=PINGORA_AI_LISTEN_PORT={listen_port}
ExecStart={binary_path} --config {config_path} --port {listen_port}
Restart=always
RestartSec=2
LimitNOFILE=65535
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
"""
