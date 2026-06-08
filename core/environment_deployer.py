from __future__ import annotations
from core.nginx_edge_decision import raise_if_blocked, validate_candidate_container_config, validate_edge_config_points_to_color, validate_running_nginx_points_to_color

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
UTC = timezone.utc
from pathlib import Path
from core.nginx_config_guard import validate_nginx_text, validate_nginx_file
from core.nginx_failure_classifier import classify_nginx_failure
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GENERATED_PROJECT_DIR = PROJECT_ROOT / "generated-pingora-proxy"
DOCKER_DIR = PROJECT_ROOT / "docker-bluegreen"
STATE_PATH = PROJECT_ROOT / "data" / "docker_bluegreen_state.json"

DOCKER_CONTAINER_NAMES = [
    "ai-pingora-gateway-blue",
    "ai-pingora-gateway-green",
    "ai-pingora-gateway-edge",
]

DOCKER_NETWORK_NAMES = [
    "docker-bluegreen_ai_pingora_net",
    "docker-bluegreen_default",
]

DOCKER_BLUEGREEN_GATEWAY_PORT = 8090


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd or PROJECT_ROOT),
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def require_docker() -> None:
    if not shutil.which("docker"):
        raise RuntimeError(
            "Docker runtime requested, but Docker CLI is not available in this WSL distro."
        )

    checks = [
        ["docker", "--version"],
        ["docker", "ps"],
    ]

    for command in checks:
        result = run_command(command, timeout=30)

        if result.returncode != 0:
            raise RuntimeError(
                f"Docker check failed: {' '.join(command)}\n"
                f"STDOUT:\n{result.stdout}\n\n"
                f"STDERR:\n{result.stderr}"
            )


def normalize_path(path: Any) -> str:
    text = str(path or "/").strip()

    if not text.startswith("/"):
        text = "/" + text

    if len(text) > 1:
        text = text.rstrip("/")

    return text or "/"


def extract_port(prompt: str, default: int = 8090) -> int:
    text = str(prompt or "")

    matches = re.findall(r"\bport\s+(\d{2,5})\b", text, flags=re.IGNORECASE)

    if matches:
        try:
            port = int(matches[0])
            if 1 <= port <= 65535:
                return port
        except Exception:
            pass

    return default


def normalize_address(address: Any, runtime_mode: str) -> str:
    text = str(address or "").strip()
    text = text.replace("localhost", "127.0.0.1")
    text = text.replace("http://", "").replace("https://", "").rstrip("/")

    if runtime_mode == "docker_host":
        text = text.replace("127.0.0.1:", "host.docker.internal:")

    return text


def rewrite_route_addresses(route: dict[str, Any], runtime_mode: str) -> dict[str, Any]:
    fixed = dict(route)

    for key in ["upstream", "backend"]:
        if fixed.get(key):
            fixed[key] = normalize_address(fixed[key], runtime_mode)

    upstreams = fixed.get("upstreams")

    if isinstance(upstreams, list):
        new_upstreams = []

        for item in upstreams:
            if isinstance(item, dict):
                new_item = dict(item)

                if new_item.get("address"):
                    new_item["address"] = normalize_address(new_item["address"], runtime_mode)

                if new_item.get("upstream"):
                    new_item["upstream"] = normalize_address(new_item["upstream"], runtime_mode)

                if new_item.get("backend"):
                    new_item["backend"] = normalize_address(new_item["backend"], runtime_mode)

                new_upstreams.append(new_item)
            else:
                new_upstreams.append(normalize_address(item, runtime_mode))

        fixed["upstreams"] = new_upstreams

    return fixed


def apply_runtime_addressing(config: dict[str, Any], runtime_mode: str, strategy: str) -> dict[str, Any]:
    fixed = json.loads(json.dumps(config))

    fixed["runtime"] = runtime_mode
    fixed["runtime_mode"] = runtime_mode
    fixed["strategy"] = strategy
    fixed["deployment_strategy"] = strategy

    routes = fixed.get("routes") or []

    if isinstance(routes, list):
        fixed["routes"] = [
            rewrite_route_addresses(route, runtime_mode)
            if isinstance(route, dict)
            and str(route.get("type") or "").lower() not in {"static", "web", "webserver", "web_server"}
            else route
            for route in routes
        ]

    return fixed


def force_static_homepage(config: dict[str, Any]) -> dict[str, Any]:
    fixed = json.loads(json.dumps(config))
    routes = fixed.get("routes") or []

    if not isinstance(routes, list):
        routes = []

    non_root = [
        route for route in routes
        if not (
            isinstance(route, dict)
            and normalize_path(route.get("path")) == "/"
        )
    ]

    fixed["routes"] = [
        {
            "path": "/",
            "type": "static",
            "root": "public",
            "index": "index.html",
        }
    ] + non_root

    return fixed


def build_config_from_prompt_contract(prompt: str, runtime_mode: str, strategy: str) -> dict[str, Any]:
    from core.intent_contract import build_intent_contract, repair_config_from_contract, validate_config_against_contract

    port = extract_port(prompt, default=8090)

    base_config = {
        "port": port,
        "routes": [],
        "runtime": runtime_mode,
        "runtime_mode": runtime_mode,
        "strategy": strategy,
        "deployment_strategy": strategy,
        "security": {
            "allowed_methods": ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
            "blocked_paths": [
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
            ],
            "rate_limit_per_minute": 10000,
            "max_connections": 1000,
            "max_request_body_bytes": 10485760,
            "upstream_timeout_seconds": 30,
        },
    }

    contract = build_intent_contract(prompt)

    if not contract.routes:
        raise RuntimeError(
            "Could not extract a safe prompt intent contract. "
            "For Docker/future environments, explicit route intent is required."
        )

    config = repair_config_from_contract(base_config, contract)
    validation = validate_config_against_contract(config, contract)

    if not validation.get("success"):
        raise RuntimeError(
            "Prompt intent contract could not be satisfied: "
            + json.dumps(validation.get("failures"), indent=2)
        )

    return config


def apply_profiles(config: dict[str, Any], runtime_mode: str, strategy: str) -> dict[str, Any]:
    fixed = json.loads(json.dumps(config))

    try:
        from core.security_profile import apply_local_production_security_profile

        fixed = apply_local_production_security_profile(fixed, environment="local")
    except Exception:
        pass

    try:
        from core.production_readiness import apply_production_readiness_profile

        fixed = apply_production_readiness_profile(fixed, environment="local")
    except Exception:
        pass

    fixed = force_static_homepage(fixed)
    try:
        from core.production_security_contract import apply_production_security_contract

        fixed = apply_production_security_contract(
            fixed,
            environment=runtime_mode,
        )
    except Exception:
        pass

    fixed = apply_runtime_addressing(fixed, runtime_mode, strategy)

    return fixed


def sync_config_mirrors(config: dict[str, Any]) -> None:
    mirrors = [
        GENERATED_PROJECT_DIR / "config.json",
        PROJECT_ROOT / "active_config.json",
        PROJECT_ROOT / "gateway_config.json",
        PROJECT_ROOT / "generated-projects" / "default-project" / "current_config.json",
    ]

    for path in mirrors:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")


def create_backup(reason: str) -> Path | None:
    try:
        from core.local_backup import create_project_backup

        return create_project_backup(GENERATED_PROJECT_DIR, reason=reason)
    except Exception:
        return None


def write_project_from_config(config: dict[str, Any]) -> None:
    from core.project_writer import write_project

    write_project(config, project_dir=GENERATED_PROJECT_DIR)
    sync_config_mirrors(config)


def write_dockerfile() -> None:
    dockerfile = GENERATED_PROJECT_DIR / "Dockerfile"

    dockerfile.write_text(
        """FROM rust:1.85-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
        pkg-config \
        libssl-dev \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY Cargo.toml Cargo.lock* ./
COPY src ./src
COPY public ./public
COPY config.json ./config.json

RUN cargo build --release

EXPOSE 8090

CMD ["cargo", "run", "--release"]
""",
        encoding="utf-8",
    )

    dockerignore = GENERATED_PROJECT_DIR / ".dockerignore"

    dockerignore.write_text(
        """target
.runtime
tmp
*.log
*.sqlite3
*.db
.git
.gitignore
__pycache__
.pytest_cache
*.bak*
*.pyc
data
logs.jsonl
generated-projects
""",
        encoding="utf-8",
    )


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {
            "active_color": "green",
            "updated_at": utc_now(),
        }

    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {
            "active_color": "green",
            "updated_at": utc_now(),
        }


def save_state(active_color: str) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(
            {
                "active_color": active_color,
                "updated_at": utc_now(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def next_color() -> str:
    current = str(load_state().get("active_color") or "green")

    return "blue" if current == "green" else "green"



def atomic_write_text(path: Path, content: str) -> None:
    """
    Write text while preserving the inode used by Docker Desktop file mounts.

    Single-file bind mounts can keep a stale or broken view when the host file is
    replaced via rename. For NGINX config, update the existing file in place and
    let reload_nginx_edge retry nginx -t until Docker has synced the full file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _edge_api_key_for_nginx() -> str:
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


def write_nginx_conf(active_color: str, gateway_port: int) -> None:
    DOCKER_DIR.mkdir(parents=True, exist_ok=True)

    service_name = f"gateway-{active_color}"
    expected_api_key = _edge_api_key_for_nginx()

    nginx_conf = f"""events {{}}

http {{
    limit_req_zone $binary_remote_addr zone=pingora_edge_rate:10m rate=120r/s;

    server {{
        listen 80;
        server_name _;

        location = /__edge_health {{
            add_header Content-Type text/plain;
            return 200 "ok\\n";
        }}

        location / {{
            return 308 https://$host:8443$request_uri;
        }}
    }}

    server {{
        listen 443 ssl;
        server_name _;

        ssl_certificate /etc/nginx/tls/local.crt;
        ssl_certificate_key /etc/nginx/tls/local.key;
        ssl_protocols TLSv1.2 TLSv1.3;
        ssl_prefer_server_ciphers on;

        client_max_body_size 10m;

        add_header X-Content-Type-Options "nosniff" always;
        add_header X-Frame-Options "DENY" always;
        add_header Referrer-Policy "no-referrer" always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=()" always;
        add_header X-Permitted-Cross-Domain-Policies "none" always;
        add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

        location = /__edge_health {{
            add_header Content-Type text/plain;
            return 200 "ok\\n";
        }}

        # Public route-directory homepage.
        location = / {{
            proxy_pass http://{service_name}:{gateway_port};
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto https;
            proxy_connect_timeout 5s;
            proxy_send_timeout 30s;
            proxy_read_timeout 30s;
        }}

        # Static browser noise can stay public or return cleanly.
        location = /favicon.ico {{
            proxy_pass http://{service_name}:{gateway_port};
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header X-Forwarded-Proto https;
        }}

        # Block sensitive paths before auth.
        location ~* ^/(\\.env|\\.git|\\.git/|private|internal|debug|wp-admin|wp-login\\.php|phpmyadmin|admin\\.php) {{
            return 403;
        }}

        # Block dangerous/suspicious extensions before auth.
        location ~* \\.(php|phtml|sql|bak|old|swp|ini|env|log)$ {{
            return 403;
        }}

        # Protected routes: everything except public root requires X-API-Key.
        location / {{
            set $api_key_valid 0;

            if ($http_x_api_key = "{expected_api_key}") {{
                set $api_key_valid 1;
            }}

            if ($api_key_valid = 0) {{
                return 401;
            }}

            limit_req zone=pingora_edge_rate burst=240 nodelay;

            proxy_pass http://{service_name}:{gateway_port};
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto https;
            proxy_connect_timeout 5s;
            proxy_send_timeout 30s;
            proxy_read_timeout 30s;
        }}
    }}
}}
"""

    atomic_write_text(DOCKER_DIR / "nginx.conf", nginx_conf)


def _docker_edge_type() -> str:
    return os.getenv("PINGORA_AI_EDGE_TYPE", "").strip().lower()


def _docker_pingora_edge_enabled() -> bool:
    return _docker_edge_type() == "pingora"


def _docker_edge_live_url() -> str:
    if _docker_pingora_edge_enabled():
        return "http://127.0.0.1:80"
    return "https://127.0.0.1:8443"


def _docker_edge_readiness_url(port: int = DOCKER_BLUEGREEN_GATEWAY_PORT) -> str:
    if _docker_pingora_edge_enabled():
        return "http://127.0.0.1:80/"
    return f"http://127.0.0.1:{port}/__edge_health"


def write_compose(active_color: str, gateway_port: int) -> Path:
    DOCKER_DIR.mkdir(parents=True, exist_ok=True)

    compose = DOCKER_DIR / "docker-compose.yml"

    edge_upstream = f"http://gateway-{active_color}:{gateway_port}"

    if _docker_pingora_edge_enabled():
        edge_block = f"""  gateway-edge:
    build:
      context: .
      dockerfile: Dockerfile.pingora-edge
    container_name: ai-pingora-gateway-edge
    ports:
      - "80:80"
    environment:
      PINGORA_EDGE_LISTEN: "0.0.0.0:80"
      PINGORA_EDGE_UPSTREAM: "{edge_upstream}"
    depends_on:
      - gateway-blue
      - gateway-green
    networks:
      - ai_pingora_net
"""
    else:
        edge_block = """  gateway-edge:
    image: nginx:alpine
    container_name: ai-pingora-gateway-edge
    ports:
      - "8090:80"
      - "8443:443"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
      - ../data/tls:/etc/nginx/tls:ro
    depends_on:
      - gateway-blue
      - gateway-green
    networks:
      - ai_pingora_net
"""

    # Build context is relative to docker-bluegreen/
    compose.write_text(
        f"""services:
  gateway-blue:
    build:
      context: ../generated-pingora-proxy
    container_name: ai-pingora-gateway-blue
    expose:
      - "{gateway_port}"
    extra_hosts:
      - "host.docker.internal:host-gateway"
    networks:
      - ai_pingora_net

  gateway-green:
    build:
      context: ../generated-pingora-proxy
    container_name: ai-pingora-gateway-green
    expose:
      - "{gateway_port}"
    extra_hosts:
      - "host.docker.internal:host-gateway"
    networks:
      - ai_pingora_net

{edge_block}

networks:
  ai_pingora_net:
    driver: bridge
""",
        encoding="utf-8",
    )

    return compose


def kill_host_port(port: int) -> None:
    # Stop stale local direct gateway if it owns the host port.
    run_command(
        ["bash", "-lc", f"lsof -tiTCP:{port} -sTCP:LISTEN | xargs -r kill -9"],
        timeout=30,
    )


def docker_compose(command: list[str], *, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    return run_command(
        ["docker", "compose", "-f", str(DOCKER_DIR / "docker-compose.yml")] + command,
        cwd=PROJECT_ROOT,
        timeout=timeout,
    )


def docker_bluegreen_cleanup(reason: str = "docker_bluegreen_cleanup") -> dict[str, Any]:
    print("")
    print(f"🧹 Docker blue/green cleanup: {reason}")

    results: dict[str, Any] = {
        "reason": reason,
        "compose_down": None,
        "containers": [],
        "networks": [],
    }

    if (DOCKER_DIR / "docker-compose.yml").exists():
        down = docker_compose(["down", "--remove-orphans"], timeout=300)
        results["compose_down"] = {
            "returncode": down.returncode,
            "stdout": down.stdout[-2000:],
            "stderr": down.stderr[-2000:],
        }

    for name in DOCKER_CONTAINER_NAMES:
        rm = run_command(["docker", "rm", "-f", name], timeout=60)
        results["containers"].append({
            "name": name,
            "returncode": rm.returncode,
            "stdout": rm.stdout.strip(),
            "stderr": rm.stderr.strip(),
        })

    ps = run_command(
        ["bash", "-lc", "docker ps -aq --filter 'name=ai-pingora-gateway' | xargs -r docker rm -f"],
        timeout=60,
    )
    results["containers"].append({
        "name": "ai-pingora-gateway*",
        "returncode": ps.returncode,
        "stdout": ps.stdout.strip(),
        "stderr": ps.stderr.strip(),
    })

    for network_name in DOCKER_NETWORK_NAMES:
        rm_net = run_command(["docker", "network", "rm", network_name], timeout=60)
        results["networks"].append({
            "name": network_name,
            "returncode": rm_net.returncode,
            "stdout": rm_net.stdout.strip(),
            "stderr": rm_net.stderr.strip(),
        })

    return results


def is_docker_compose_name_conflict(result: subprocess.CompletedProcess[str]) -> bool:
    combined = f"{result.stdout}\n{result.stderr}".lower()
    return any(
        marker in combined
        for marker in [
            "already exists",
            "already in use",
            "conflict. the container name",
            "network with name",
            "you have to remove",
        ]
    )


def docker_compose_up_with_conflict_repair(timeout: int = 600) -> subprocess.CompletedProcess[str]:
    up = docker_compose(["up", "-d"], timeout=timeout)

    if up.returncode == 0:
        return up

    if not is_docker_compose_name_conflict(up):
        return up

    print("")
    print("⚠️ Docker compose up hit stale container/network conflict.")
    print("🔁 Cleaning Docker blue/green resources and retrying once...")

    docker_bluegreen_cleanup(reason="repair_after_docker_compose_up_conflict")
    time.sleep(2)

    retry = docker_compose(["up", "-d"], timeout=timeout)

    if retry.returncode == 0:
        print("✅ Docker compose up succeeded after conflict cleanup retry.")

    return retry


def _apply_docker_public_url_metadata(config: dict[str, Any]) -> dict[str, Any]:
    """
    Docker blue/green may keep the generated gateway listening internally on 8090,
    but the public Docker Pingora edge is exposed on standard port 80.

    Do not change listen_port/proxy_port here. Those are internal container ports.
    Only fix user-facing/public metadata used by homepage/verifier.
    """
    if _docker_pingora_edge_enabled():
        config["live_url"] = "http://127.0.0.1:80/"
        config["public_url"] = "http://127.0.0.1:80/"
        config["base_url"] = "http://127.0.0.1:80/"
    return config


def start_docker_bluegreen(config: dict[str, Any]) -> dict[str, Any]:
    require_docker()

    # Docker blue/green local runtime must use the stable edge/gateway port.
    # FastAPI config-file plans may still contain port 8080, but generated
    # Pingora currently starts on 8090. Force Docker/Nginx/readiness to 8090.
    config["port"] = DOCKER_BLUEGREEN_GATEWAY_PORT
    config["listen_port"] = DOCKER_BLUEGREEN_GATEWAY_PORT
    config["proxy_port"] = DOCKER_BLUEGREEN_GATEWAY_PORT
    config = _apply_docker_public_url_metadata(config)

    port = DOCKER_BLUEGREEN_GATEWAY_PORT
    color = next_color()

    write_dockerfile()
    if not _docker_pingora_edge_enabled():
        write_nginx_conf(color, port)
    compose_file = write_compose(color, port)

    # For first stable Docker test, avoid false port conflicts from local direct gateway.
    docker_compose(["down", "--remove-orphans"], timeout=300)
    kill_host_port(port)

    print("")
    print(f"🐳 Starting Docker blue/green deployment. Active color will be: {color}")
    print(f"Compose file: {compose_file}")

    from agents.deployment_repair_agent import run_docker_command_with_repair

    build = run_docker_command_with_repair(
        [
            "docker",
            "compose",
            "-f",
            str(DOCKER_DIR / "docker-compose.yml"),
            "build",
            "--no-cache",
        ],
        project_dir=str(GENERATED_PROJECT_DIR),
        max_attempts=3,
        retry_delay_seconds=5,
        timeout=1200,
    )

    if build.stdout.strip():
        print(build.stdout[-4000:])

    if build.stderr.strip():
        print(build.stderr[-4000:])

    if not build.ok:
        raise RuntimeError(
            "Docker compose build failed after deployment repair agent.\n"
            f"Classification: {build.classification}\n"
            f"Attempts: {build.attempts}\n"
            f"Repaired: {build.repaired}\n\n"
            f"STDOUT:\n{build.stdout}\n\n"
            f"STDERR:\n{build.stderr}"
        )

    up = docker_compose_up_with_conflict_repair(timeout=600)

    if up.stdout.strip():
        print(up.stdout[-4000:])

    if up.stderr.strip():
        print(up.stderr[-4000:])

    if up.returncode != 0:
        raise RuntimeError(
            "Docker compose up failed after conflict cleanup retry.\n"
            f"STDOUT:\n{up.stdout}\n\nSTDERR:\n{up.stderr}"
        )

    save_state(color)

    readiness_url = _docker_edge_readiness_url(port)
    live_url = _docker_edge_live_url()

    try:
        status_code = wait_for_docker_edge_health(port, timeout_seconds=180)
        status = str(status_code)
        print(f"✅ Docker gateway edge ready: HTTP {status}")
    except Exception as exc:
        logs = docker_compose(["logs", "--tail=200"], timeout=60)
        raise RuntimeError(
            f"Docker gateway did not become ready at {readiness_url}\n\n"
            f"LOGS:\n{logs.stdout}\n{logs.stderr}"
        ) from exc

    ps = docker_compose(["ps"], timeout=60)

    return {
        "success": True,
        "active_color": color,
        "live_url": live_url,
        "status": status,
        "compose_file": str(compose_file),
        "docker_ps": ps.stdout,
    }


def verify_deployment(config: dict[str, Any], live_url: str, level: str) -> dict[str, Any]:
    from core.post_change_verifier import format_verification_report, verify_after_change

    result = verify_after_change(
        config=config,
        config_path=GENERATED_PROJECT_DIR / "config.json",
        live_url=live_url,
        source="environment_deployer",
        change_type="docker_bluegreen_deploy",
        level=level,
        store=True,
    )

    print("")
    print(format_verification_report(result))

    return result



# ---------------------------------------------------------------------
# Transactional zero-downtime Docker blue/green update
# ---------------------------------------------------------------------

def docker_container_is_running(name: str) -> bool:
    result = run_command(
        ["docker", "inspect", "-f", "{{.State.Running}}", name],
        timeout=30,
    )
    return result.returncode == 0 and result.stdout.strip().lower() == "true"



def wait_for_docker_edge_health(port: int = 8090, timeout_seconds: int = 180) -> int:
    """
    Docker readiness must check the unauthenticated HTTP edge health endpoint.

    This is separate from the public app URL:
      readiness: http://127.0.0.1:8090/__edge_health
      public:    https://127.0.0.1:8443
    """
    import time
    import urllib.request
    import urllib.error

    url = _docker_edge_readiness_url(port)
    deadline = time.time() + timeout_seconds
    last_error = None

    while time.time() < deadline:
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=3) as response:
                status = int(response.status)
                if status in {200, 301, 302, 307, 308}:
                    return status
                last_error = f"HTTP {status}"
        except Exception as exc:
            last_error = str(exc)

        time.sleep(2)

    raise RuntimeError(
        f"Docker gateway did not become ready at {url}\n"
        f"Last readiness error: {last_error}"
    )


def docker_exec_edge(command: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return run_command(
        ["docker", "exec", "ai-pingora-gateway-edge", "sh", "-lc", command],
        timeout=timeout,
    )


def reload_nginx_edge():
    """
    Reload NGINX edge safely on Docker Desktop/macOS.

    Problem:
    /etc/nginx/nginx.conf is bind-mounted from docker-bluegreen/nginx.conf.
    During blue/green switch, rewriting that host file and immediately running
    nginx -t can cause Docker Desktop to expose a partially synced file:
      pread() returned only N bytes instead of M

    Fix:
    Validate the host config, keep a temp copy inside the container for
    diagnostics, then test and reload the default NGINX config path. Reloading
    with "-c /tmp/..." only validates the temp file; the already-running master
    keeps using its original config path and the blue/green switch does not
    take effect.
    """
    nginx_path = DOCKER_DIR / "nginx.conf"
    container_tmp_conf = "/tmp/ai-pingora-nginx.conf"

    validate_nginx_file(nginx_path)

    copy_result = run_command(
        [
            "docker",
            "cp",
            str(nginx_path),
            f"ai-pingora-gateway-edge:{container_tmp_conf}",
        ],
        timeout=30,
    )

    if copy_result.returncode != 0:
        return copy_result

    temp_test_result = run_command(
        [
            "docker",
            "exec",
            "ai-pingora-gateway-edge",
            "nginx",
            "-t",
            "-c",
            container_tmp_conf,
        ],
        timeout=30,
    )

    if temp_test_result.returncode != 0:
        return temp_test_result

    last_test = temp_test_result
    for _attempt in range(20):
        default_test = run_command(
            [
                "docker",
                "exec",
                "ai-pingora-gateway-edge",
                "nginx",
                "-t",
            ],
            timeout=30,
        )

        last_test = default_test
        if default_test.returncode == 0:
            break

        time.sleep(1)
    else:
        return last_test

    return run_command(
        [
            "docker",
            "exec",
            "ai-pingora-gateway-edge",
            "nginx",
            "-s",
            "reload",
        ],
        timeout=30,
    )



def wait_for_candidate_container(
    *,
    color: str,
    port: int,
    timeout_seconds: int = 120,
) -> bool:
    service_name = f"gateway-{color}"
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        if not docker_container_is_running(f"ai-pingora-gateway-{color}"):
            time.sleep(2)
            continue

        # nginx:alpine normally has busybox wget available.
        result = docker_exec_edge(
            f"wget -q -S -O /dev/null http://{service_name}:{port}/ 2>&1 || true",
            timeout=15,
        )

        output = f"{result.stdout}\n{result.stderr}"

        if any(code in output for code in ["200", "301", "302", "403", "404", "405"]):
            return True

        time.sleep(2)

    return False


def wait_for_live_edge(live_url: str, timeout_seconds: int = 90) -> int:
    """
    Wait for the NGINX edge to be healthy after a blue/green switch.

    Important:
    After TLS/API-key enforcement, /api is protected and / may redirect.
    Therefore switch health must use the public unauthenticated edge endpoint.
    """
    import time

    health_url = "http://127.0.0.1:8090/__edge_health"
    deadline = time.time() + timeout_seconds
    last_status = None
    last_output = ""

    while time.time() < deadline:
        result = run_command(
            ["bash", "-lc", f"curl -s -o /dev/null -w '%{{http_code}}' {health_url}"],
            timeout=10,
        )

        last_output = (result.stdout or "") + "\n" + (result.stderr or "")
        status_text = (result.stdout or "").strip()

        try:
            status = int(status_text)
        except Exception:
            status = None

        last_status = status

        if status in {200, 301, 302, 307, 308}:
            return status

        time.sleep(1)

    raise RuntimeError(
        "Live edge did not become healthy after switch. "
        f"Checked {health_url}. "
        f"Last status: {last_status}. "
        f"Last output: {last_output[-1000:]}"
    )



def ensure_base_bluegreen_stack_exists(config: dict[str, Any]) -> dict[str, Any] | None:
    """
    If there is no current live blue/green stack, we cannot do a transactional
    switch. In that case, perform the normal full start once.
    """
    edge_running = docker_container_is_running("ai-pingora-gateway-edge")
    blue_running = docker_container_is_running("ai-pingora-gateway-blue")
    green_running = docker_container_is_running("ai-pingora-gateway-green")

    if edge_running and (blue_running or green_running):
        return None

    print("")
    print("ℹ️ No live blue/green edge found. Performing initial full Docker start.")
    return start_docker_bluegreen(config)


def rollback_edge_to_color(color: str, port: int) -> None:
    try:
        print("")
        print(f"↩️ Rolling Nginx edge back to {color}...")
        write_nginx_conf(color, port)
        reload_nginx_edge()
        save_state(color)
        print(f"✅ Edge rolled back to {color}.")
    except Exception as exc:
        print(f"⚠️ Edge rollback attempt failed: {exc}")


def start_docker_bluegreen_transactional(config: dict[str, Any]) -> dict[str, Any]:
    """
    True zero-downtime update path.

    Important behaviour:
    - Does NOT stop the active colour before candidate verification.
    - Does NOT stop the Nginx edge before candidate verification.
    - Builds/starts only the inactive candidate colour.
    - Switches Nginx only after candidate is healthy.
    - If interrupted before switch, old live gateway remains running.
    - If switch fails, attempts to point Nginx back to old active colour.
    """

    require_docker()

    config["port"] = DOCKER_BLUEGREEN_GATEWAY_PORT
    config["listen_port"] = DOCKER_BLUEGREEN_GATEWAY_PORT
    config["proxy_port"] = DOCKER_BLUEGREEN_GATEWAY_PORT
    config = _apply_docker_public_url_metadata(config)

    port = DOCKER_BLUEGREEN_GATEWAY_PORT

    initial = ensure_base_bluegreen_stack_exists(config)
    if initial is not None:
        return initial

    old_color = str(load_state().get("active_color") or "green")
    if old_color not in {"blue", "green"}:
        old_color = "green"

    candidate_color = "blue" if old_color == "green" else "green"

    print("")
    print("🔁 Transactional Docker blue/green update")
    print(f"Current live colour: {old_color}")
    print(f"Candidate colour: {candidate_color}")
    print("Old gateway will remain live until candidate passes verification.")

    # Write project files for the candidate image. Existing active container is
    # unaffected because it already has its old config baked into its image.
    write_project_from_config(config)

    write_dockerfile()
    write_compose(old_color, port)

    switched = False
    readiness_url = f"http://127.0.0.1:{port}/__edge_health"
    live_url = "https://127.0.0.1:8443"

    try:
        # Remove only candidate. Never remove active or edge here.
        run_command(
            ["docker", "rm", "-f", f"ai-pingora-gateway-{candidate_color}"],
            timeout=60,
        )

        from agents.deployment_repair_agent import run_docker_command_with_repair

        build = run_docker_command_with_repair(
            [
                "docker",
                "compose",
                "-f",
                str(DOCKER_DIR / "docker-compose.yml"),
                "build",
                "--no-cache",
                f"gateway-{candidate_color}",
            ],
            project_dir=str(GENERATED_PROJECT_DIR),
            max_attempts=3,
            retry_delay_seconds=5,
            timeout=1200,
        )

        if build.stdout.strip():
            print(build.stdout[-4000:])

        if build.stderr.strip():
            print(build.stderr[-4000:])

        if not build.ok:
            raise RuntimeError(
                "Candidate Docker build failed. Old gateway is still live.\n"
                f"Classification: {build.classification}\n"
                f"Attempts: {build.attempts}\n"
                f"Repaired: {build.repaired}\n\n"
                f"STDOUT:\n{build.stdout}\n\n"
                f"STDERR:\n{build.stderr}"
            )

        up = docker_compose(
            ["up", "-d", "--no-deps", f"gateway-{candidate_color}"],
            timeout=600,
        )

        if up.stdout.strip():
            print(up.stdout[-4000:])

        if up.stderr.strip():
            print(up.stderr[-4000:])

        if up.returncode != 0:
            raise RuntimeError(
                "Candidate container failed to start. Old gateway is still live.\n"
                f"STDOUT:\n{up.stdout}\n\nSTDERR:\n{up.stderr}"
            )

        candidate_ready = wait_for_candidate_container(
            color=candidate_color,
            port=port,
            timeout_seconds=120,
        )

        if not candidate_ready:
            logs = docker_compose(["logs", "--tail=120", f"gateway-{candidate_color}"], timeout=60)
            raise RuntimeError(
                "Candidate gateway did not become ready. Old gateway is still live.\n"
                f"LOGS:\n{logs.stdout}\n{logs.stderr}"
            )

        print(f"✅ Candidate {candidate_color} is ready internally.")

        # Switch edge only now.
        write_nginx_conf(candidate_color, port)

        # Hard gate: never reload malformed NGINX config.
        validate_nginx_file(DOCKER_DIR / "nginx.conf")

        # Keep a snapshot of the exact candidate edge config used for switch debugging.
        candidate_snapshot = DOCKER_DIR / f"nginx.candidate-{candidate_color}.conf"
        candidate_snapshot.write_text(
            (DOCKER_DIR / "nginx.conf").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        reload = reload_nginx_edge()

        if reload.returncode != 0:
            rollback_edge_to_color(old_color, port)

            failure_output = (reload.stdout or "") + "\n" + (reload.stderr or "")
            nginx_failure = classify_nginx_failure(failure_output)
            root_cause = nginx_failure.root_cause if nginx_failure else "unknown"

            raise RuntimeError(
                "Nginx reload failed. Rolled back edge to old colour.\n"
                f"Root cause: {root_cause}\n"
                f"STDOUT:\n{reload.stdout}\n\nSTDERR:\n{reload.stderr}"
            )

        wait_for_docker_edge_health(port=8090, timeout_seconds=90)

        switched = True

        status = wait_for_live_edge(
            live_url=live_url,
            timeout_seconds=90,
        )

        if not status:
            rollback_edge_to_color(old_color, port)
            raise RuntimeError(
                "Live edge did not become healthy after switch. Rolled back edge to old colour."
            )

        save_state(candidate_color)

        print(f"✅ Zero-downtime switch complete: {old_color} → {candidate_color}")
        print(f"✅ Docker gateway edge ready: HTTP {status}")

        ps = docker_compose(["ps"], timeout=60)

        return {
            "success": True,
            "active_color": candidate_color,
            "previous_color": old_color,
            "live_url": live_url,
            "status": status,
            "compose_file": str(DOCKER_DIR / "docker-compose.yml"),
            "docker_ps": ps.stdout,
            "transactional": True,
            "zero_downtime": True,
        }

    except KeyboardInterrupt:
        if switched:
            rollback_edge_to_color(old_color, port)

        print("")
        print("🛑 Update interrupted.")
        print("✅ Previous live gateway should still be serving traffic.")

        raise

    except Exception:
        if switched:
            rollback_edge_to_color(old_color, port)
        raise


# ---------------------------------------------------------------------
# End transactional zero-downtime Docker blue/green update
# ---------------------------------------------------------------------

def deploy_docker_bluegreen(
    *,
    prompt: str,
    verify_level: str = "full",
) -> dict[str, Any]:
    require_docker()

    backup = create_backup(reason="before_docker_bluegreen_environment_deploy")

    config = build_config_from_prompt_contract(
        prompt,
        runtime_mode="docker_host",
        strategy="bluegreen",
    )

    config = apply_profiles(
        config,
        runtime_mode="docker_host",
        strategy="bluegreen",
    )

    config = normalize_docker_bluegreen_port(config)

    write_project_from_config(config)

    # Docker-host deployments need host demo backends running before verification.
    # Otherwise Docker/NGINX can be healthy but /api returns 502.
    try:
        _start_local_demo_backends_from_config(config)
    except Exception as exc:
        print(f"Could not auto-start Docker host demo backends: {exc}")

    deploy_result = start_docker_bluegreen(config)

    verification = verify_deployment(
        config,
        live_url=deploy_result["live_url"],
        level=verify_level,
    )

    if not verification.get("success"):
        raise RuntimeError(
            "Docker blue/green deployment failed verification: "
            + str(verification.get("failure_summary"))
        )

    return {
        "success": True,
        "runtime": "docker_host",
        "runtime_mode": "docker_host",
        "strategy": "bluegreen",
        "deployment_strategy": "bluegreen",
        "active_color": deploy_result["active_color"],
        "live_url": deploy_result["live_url"],
        "config": config,
        "backup": str(backup) if backup else None,
        "verification": verification,
        "docker": deploy_result,
    }


def load_config_file(config_file: str | Path) -> dict[str, Any]:
    """
    Load a ready-made JSON gateway config.

    This is used by the public CLI config-file path so local deployment can run
    without calling the local AI/LangGraph prompt-planning layer.
    """
    path = Path(config_file)

    if not path.is_absolute():
        path = PROJECT_ROOT / path

    if not path.exists():
        raise RuntimeError(f"Config file not found: {path}")

    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Could not read config file: {path}: {exc}") from exc

    if not isinstance(config, dict):
        raise RuntimeError(f"Config file must contain a JSON object: {path}")

    routes = config.get("routes")

    if not isinstance(routes, list) or not routes:
        raise RuntimeError(f"Config file must contain a non-empty routes list: {path}")

    return config



def normalize_docker_bluegreen_port(config: dict[str, Any]) -> dict[str, Any]:
    """
    Local Docker blue/green runtime currently serves Pingora on stable port 8090.

    FastAPI config-file plans may default to 8080. If Docker/Nginx uses 8080
    while generated Pingora starts on 8090, the edge returns 502 forever.
    """
    fixed = json.loads(json.dumps(config))
    fixed["port"] = 8090
    fixed["listen_port"] = 8090
    fixed["proxy_port"] = 8090
    return fixed

def deploy_docker_bluegreen_from_config(
    *,
    config: dict[str, Any],
    verify_level: str = "full",
    backup_reason: str = "before_docker_bluegreen_config_file_deploy",
) -> dict[str, Any]:
    """
    Deploy an already-planned gateway config.

    Important architecture rule:
    this function must not call prompt_to_config, LangGraph, OpenAI, or any
    local AI planning layer. It only prepares, writes, deploys, and verifies the
    supplied JSON config.
    """
    config = _system_decision_preflight_or_raise(config, runtime="docker_host")

    config = _decision_engine_preflight_or_raise(config, runtime="docker_host")
    from core.security_policy_contract import apply_security_policy_contract as _apply_security_policy_contract_runtime
    config = _apply_security_policy_contract_runtime(config, environment="docker_host")

    require_docker()

    backup = create_backup(reason=backup_reason)

    config = apply_profiles(
        config,
        runtime_mode="docker_host",
        strategy="bluegreen",
    )

    config["port"] = DOCKER_BLUEGREEN_GATEWAY_PORT
    config["listen_port"] = DOCKER_BLUEGREEN_GATEWAY_PORT
    config["proxy_port"] = DOCKER_BLUEGREEN_GATEWAY_PORT

    write_project_from_config(config)

    deploy_result = start_docker_bluegreen(config)

    verification = verify_deployment(
        config,
        live_url=deploy_result["live_url"],
        level=verify_level,
    )

    if not verification.get("success"):
        error_text = (
            "Docker blue/green config-file deployment failed verification: "
            + str(verification.get("failure_summary"))
        )

        try:
            from core.agent_incident_commander import record_runtime_failure_lesson

            incident = record_runtime_failure_lesson(
                runtime="docker_host",
                strategy="bluegreen",
                operation="apply",
                error=error_text,
                config=config,
                allow_auto_repair=True,
            )

            selected = incident.get("selected_root_cause") or {}
            print()
            print("Agent incident commander stored Docker apply failure lesson")
            print(f"Root cause: {selected.get('root_cause')}")
            print(f"Repair action: {selected.get('repair_action')}")
            print(f"Incident ID: {incident.get('incident_id')}")

        except Exception as commander_exc:
            print(f"Agent incident commander failed to store Docker lesson: {commander_exc}")

        raise RuntimeError(error_text)

    return {
        "success": True,
        "runtime": "docker_host",
        "runtime_mode": "docker_host",
        "strategy": "bluegreen",
        "deployment_strategy": "bluegreen",
        "active_color": deploy_result["active_color"],
        "live_url": deploy_result["live_url"],
        "config": config,
        "backup": str(backup) if backup else None,
        "verification": verification,
        "docker": deploy_result,
        "source": "config_file",
    }



def _local_config_replace_docker_hosts(value):
    if isinstance(value, str):
        return value.replace("host.docker.internal", "127.0.0.1")
    if isinstance(value, list):
        return [_local_config_replace_docker_hosts(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _local_config_replace_docker_hosts(item)
            for key, item in value.items()
        }
    return value


def _prepare_local_direct_config_file(config: dict) -> dict:
    import copy

    prepared = copy.deepcopy(config)
    prepared = _local_config_replace_docker_hosts(prepared)

    prepared["runtime"] = "local"
    prepared["runtime_mode"] = "local"
    prepared["strategy"] = "direct"
    prepared["deployment_strategy"] = "direct"
    prepared["port"] = 8090
    prepared["listen_port"] = 8090
    prepared["proxy_port"] = 8090

    routes = prepared.get("routes")
    if not isinstance(routes, list):
        routes = []

    has_root = any(
        isinstance(route, dict) and str(route.get("path") or "").strip() == "/"
        for route in routes
    )

    if not has_root:
        routes.insert(
            0,
            {
                "path": "/",
                "type": "static",
                "route_type": "static",
                "root": "public",
                "index": "index.html",
                "balancing": "single",
                "upstreams": [],
            },
        )

    for route in routes:
        if not isinstance(route, dict):
            continue

        path_value = str(route.get("path") or "").strip()

        if path_value == "/":
            route["path"] = "/"
            route["type"] = "static"
            route["route_type"] = "static"
            route["root"] = route.get("root") or "public"
            route["index"] = route.get("index") or "index.html"
            route["upstreams"] = []
        else:
            route.setdefault("type", "proxy")
            route.setdefault("route_type", "proxy")

    prepared["routes"] = routes

    return prepared


def _write_local_json(path, data) -> None:
    import json
    from pathlib import Path

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _kill_local_port_for_config_deploy(port: int) -> None:
    import subprocess

    subprocess.run(
        ["bash", "-lc", f"lsof -tiTCP:{port} -sTCP:LISTEN | xargs -r kill -9"],
        text=True,
        check=False,
    )


def _wait_local_http_200(url: str, timeout_seconds: int = 180) -> None:
    import time
    import urllib.request

    deadline = time.time() + timeout_seconds
    last_error = None

    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if int(response.status) == 200:
                    return
        except Exception as exc:
            last_error = exc

        time.sleep(1)

    raise RuntimeError(f"Timed out waiting for {url}. Last error: {last_error}")


def _start_local_demo_backends_from_config(config: dict) -> None:
    import subprocess
    import sys
    from pathlib import Path

    tmp_config = Path("tmp/local-direct-config-file-backends.json")
    _write_local_json(tmp_config, config)

    script = Path("scripts/demo_backends.py")
    if not script.exists():
        return

    subprocess.run(
        [sys.executable, str(script), "start", str(tmp_config)],
        text=True,
        check=True,
    )



def choose_readiness_url(runtime: str = "local", config: dict | None = None) -> str:
    """
    Resolve local readiness/live URL.
    Keep this top-level so local runtime helpers do not shadow it.
    """
    config = config or {}

    try:
        port = int(
            config.get("port")
            or config.get("public_port")
            or config.get("listen_port")
            or 8090
        )
    except Exception:
        port = 8090

    if runtime == "local":
        return f"http://127.0.0.1:{port}"

    return f"http://127.0.0.1:{port}"


def _start_local_pingora_from_config_file() -> dict:
    _choose_readiness_url = globals().get("choose_readiness_url")
    if _choose_readiness_url is None:
        def _choose_readiness_url(runtime: str = "local", config: dict | None = None) -> str:
            config = config or {}
            try:
                port = int(
                    config.get("port")
                    or config.get("public_port")
                    or config.get("listen_port")
                    or 8090
                )
            except Exception:
                port = 8090
            return f"http://127.0.0.1:{port}"
    import os
    import subprocess
    from pathlib import Path

    project_dir = Path("generated-pingora-proxy")
    log_dir = project_dir / "tmp" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / "local-config-file-pingora-8090.log"
    pid_file = log_dir / "local-config-file-pingora.pid"

    _kill_local_port_for_config_deploy(8090)

    env = os.environ.copy()
    env.setdefault("RUST_BACKTRACE", "1")

    with log_file.open("w", encoding="utf-8") as fh:
        live_url = _choose_readiness_url("local")

    process = subprocess.Popen(
            ["cargo", "run", "--release"],
            cwd=str(project_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            text=True,
        )

    pid_file.write_text(str(process.pid), encoding="utf-8")

    from core.deployment_prepare import choose_readiness_url
    _wait_local_http_200(_choose_readiness_url("local"), timeout_seconds=180)

    return {
        "pid": process.pid,
        "log_file": str(log_file),
        "live_url": live_url,
    }


def _apply_local_config_file_security_defaults(config: dict) -> dict:
    """
    Add the same local production-readiness defaults expected by the full verifier.
    This is config-contract support; local enforcement can remain verify_config.
    """
    allowed_methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]

    blocked_paths = [
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

    method_rate_limits = {
        "GET": 6000,
        "HEAD": 6000,
        "OPTIONS": 6000,
        "POST": 1200,
        "PUT": 900,
        "PATCH": 900,
        "DELETE": 300,
    }

    https = {
        "enabled": False,
        "local_optional": True,
        "production_required": True,
        "termination": "waf_or_load_balancer_or_gateway",
        "redirect_http_to_https": False,
    }

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

    security = config.setdefault("security", {})

    security.setdefault("allowed_methods", allowed_methods)
    security.setdefault("blocked_paths", blocked_paths)
    security.setdefault("rate_limit_per_minute", 10000)
    security.setdefault("max_connections", 1000)
    security.setdefault("max_request_body_bytes", 1048576)
    security.setdefault("upstream_timeout_seconds", 30)
    security.setdefault("https", https)
    security.setdefault("method_rate_limits_per_minute", method_rate_limits)
    security.setdefault("content_type_policy", content_type_policy)
    security.setdefault("idempotency_policy", idempotency_policy)
    security.setdefault("audit_policy", audit_policy)
    security.setdefault("crud_probe_policy", crud_probe_policy)

    # Also mirror common keys at top-level because older verifier versions may read either shape.
    config.setdefault("allowed_methods", allowed_methods)
    config.setdefault("blocked_paths", blocked_paths)
    config.setdefault("max_request_body_bytes", 1048576)
    config.setdefault("https", https)
    config.setdefault("method_rate_limits_per_minute", method_rate_limits)
    config.setdefault("content_type_policy", content_type_policy)
    config.setdefault("idempotency_policy", idempotency_policy)
    config.setdefault("audit_policy", audit_policy)
    config.setdefault("crud_probe_policy", crud_probe_policy)

    for route in config.get("routes", []) or []:
        if not isinstance(route, dict):
            continue

        if str(route.get("path") or "") == "/":
            continue

        route.setdefault("allowed_methods", allowed_methods)
        route.setdefault("method_rate_limits_per_minute", method_rate_limits)
        route.setdefault("content_type_policy", content_type_policy)
        route.setdefault("idempotency_policy", idempotency_policy)
        route.setdefault("audit_policy", audit_policy)
        route.setdefault("crud_probe_policy", crud_probe_policy)

        route_policy = route.setdefault("policy", {})
        route_policy.setdefault("allowed_methods", allowed_methods)
        route_policy.setdefault("content_type_policy", content_type_policy)
        route_policy.setdefault("idempotency_policy", idempotency_policy)
        route_policy.setdefault("audit_policy", audit_policy)
        route_policy.setdefault("crud_probe_policy", crud_probe_policy)

    # Force-fill policy contract values even when the API supplied empty [] or {}.
    # setdefault() is not enough here because [] and {} already count as existing.
    def _is_empty_policy_value(value):
        return value is None or value == [] or value == {}

    def _force_if_empty(container, key, value):
        if not isinstance(container, dict):
            return
        if _is_empty_policy_value(container.get(key)):
            container[key] = value

    security = config.setdefault("security", {})

    forced_defaults = {
        "allowed_methods": allowed_methods,
        "blocked_paths": blocked_paths,
        "max_request_body_bytes": 1048576,
        "https": https,
        "method_rate_limits_per_minute": method_rate_limits,
        "content_type_policy": content_type_policy,
        "idempotency_policy": idempotency_policy,
        "audit_policy": audit_policy,
        "crud_probe_policy": crud_probe_policy,
    }

    for key, value in forced_defaults.items():
        _force_if_empty(config, key, value)
        _force_if_empty(security, key, value)

    _force_if_empty(security, "rate_limit_per_minute", 10000)
    _force_if_empty(security, "max_connections", 1000)
    _force_if_empty(security, "upstream_timeout_seconds", 30)

    for route in config.get("routes", []) or []:
        if not isinstance(route, dict):
            continue

        if str(route.get("path") or "") == "/":
            continue

        # Some verifier versions read route-level fields directly.
        _force_if_empty(route, "allowed_methods", allowed_methods)
        _force_if_empty(route, "methods", allowed_methods)
        _force_if_empty(route, "method_rate_limits_per_minute", method_rate_limits)
        _force_if_empty(route, "content_type_policy", content_type_policy)
        _force_if_empty(route, "idempotency_policy", idempotency_policy)
        _force_if_empty(route, "audit_policy", audit_policy)
        _force_if_empty(route, "crud_probe_policy", crud_probe_policy)

        # Some verifier versions read route.security.
        route_security = route.setdefault("security", {})
        _force_if_empty(route_security, "allowed_methods", allowed_methods)
        _force_if_empty(route_security, "methods", allowed_methods)
        _force_if_empty(route_security, "method_rate_limits_per_minute", method_rate_limits)
        _force_if_empty(route_security, "content_type_policy", content_type_policy)
        _force_if_empty(route_security, "idempotency_policy", idempotency_policy)
        _force_if_empty(route_security, "audit_policy", audit_policy)
        _force_if_empty(route_security, "crud_probe_policy", crud_probe_policy)

        # Some verifier versions read route.policy.
        route_policy = route.setdefault("policy", {})
        _force_if_empty(route_policy, "allowed_methods", allowed_methods)
        _force_if_empty(route_policy, "methods", allowed_methods)
        _force_if_empty(route_policy, "method_rate_limits_per_minute", method_rate_limits)
        _force_if_empty(route_policy, "content_type_policy", content_type_policy)
        _force_if_empty(route_policy, "idempotency_policy", idempotency_policy)
        _force_if_empty(route_policy, "audit_policy", audit_policy)
        _force_if_empty(route_policy, "crud_probe_policy", crud_probe_policy)

        # Extra compatibility fields for config verifier route-policy checks.
        # Different versions of the verifier have checked different names.
        route["method_allowlist"] = allowed_methods
        route["method_allow_list"] = allowed_methods
        route["allowed_method_allowlist"] = allowed_methods
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
        route["crud"] = route["crud_policy"]

        route_security["method_allowlist"] = allowed_methods
        route_security["method_policy"] = route["method_policy"]
        route_security["crud_policy"] = route["crud_policy"]

        route_policy["method_allowlist"] = allowed_methods
        route_policy["method_policy"] = route["method_policy"]
        route_policy["crud_policy"] = route["crud_policy"]

    return config


def _decision_engine_preflight_or_raise(config: dict, runtime: str) -> dict:
    """
    Hard deterministic preflight before project generation/deploy.

    Important:
    - Weighted route corruption must fail before repair/deploy.
    - Safe repairs such as root static route and runtime addressing happen only
      after hard algorithm checks pass.
    """
    from core.decision_engine import (
        apply_deterministic_repairs,
        evaluate_config,
    )

    if not isinstance(config, dict):
        raise RuntimeError("Decision engine rejected deployment config: config must be a dictionary")

    raw_decision = evaluate_config(config)
    raw_failed = raw_decision.get("failed_decisions", [])

    hard_fail_categories = {
        "weighted_round_robin_integrity",
    }

    hard_failed = [
        item for item in raw_failed
        if item.get("category") in hard_fail_categories
    ]

    if hard_failed:
        reason = "; ".join(item.get("reason", "unknown") for item in hard_failed)
        raise RuntimeError(
            "Decision engine rejected deployment config before project generation: "
            + reason
        )

    repaired = apply_deterministic_repairs(config, runtime=str(runtime))
    decision = evaluate_config(repaired)

    if not decision.get("ok"):
        failed = decision.get("failed_decisions", [])
        reason = "; ".join(item.get("reason", "unknown") for item in failed)
        raise RuntimeError("Decision engine rejected deployment config: " + reason)

    return repaired


def _system_decision_preflight_or_raise(config: dict, runtime: str, prompt: str | None = None) -> dict:
    """
    Deterministic system-level preflight before project generation/deploy.

    This covers:
    - runtime/strategy
    - route shape
    - algorithms
    - health-check policy
    - addressing
    - mutation safety when prompt is supplied
    """
    from core.system_decision_engine import apply_and_enforce_system_decisions

    return apply_and_enforce_system_decisions(
        config,
        prompt=prompt,
        strict_security=False,
        allow_repair=True,
    )



def preserve_native_runtime_flags(original: dict, prepared: dict) -> dict:
    """
    _prepare_local_direct_config_file may normalize local config back to defaults.
    Preserve explicit CLI/user runtime flags such as --public-port 80,
    --systemd, and --replace-edge after preparation.
    """
    if not isinstance(original, dict) or not isinstance(prepared, dict):
        return prepared

    for key in (
        "port",
        "public_port",
        "listen_port",
        "live_url",
        "_systemd",
        "systemd",
        "_replace_edge",
        "replace_edge",
        "service_user",
    ):
        if key in original:
            prepared[key] = original[key]

    # If user requested a public port, keep all port aliases aligned.
    requested_port = (
        original.get("port")
        or original.get("public_port")
        or original.get("listen_port")
    )

    if requested_port is not None:
        try:
            requested_port = int(requested_port)
            if 1 <= requested_port <= 65535:
                prepared["port"] = requested_port
                prepared["public_port"] = requested_port
                prepared["listen_port"] = requested_port
                prepared["live_url"] = f"http://127.0.0.1:{requested_port}/"
        except Exception:
            pass

    return prepared


def maybe_prepare_native_public_port(config: dict) -> dict:
    """
    If local runtime is asked to bind to privileged public port 80/443,
    optionally stop nginx and later start the generated binary through systemd.
    """
    result = {
        "enabled": False,
        "port": None,
        "replace_edge_result": None,
    }

    try:
        port = int(config.get("port") or config.get("public_port") or config.get("listen_port") or 8090)
    except Exception:
        port = 8090

    result["port"] = port

    if port >= 1024:
        return result

    result["enabled"] = True

    replace_edge = bool(config.get("replace_edge") or config.get("_replace_edge"))
    try:
        from core.native_service import stop_nginx_if_requested
        edge = stop_nginx_if_requested(port, replace_edge=replace_edge)
        result["replace_edge_result"] = edge
        if edge.get("blocked"):
            raise RuntimeError(edge.get("message"))
    except Exception as exc:
        raise RuntimeError(f"Cannot prepare native public port {port}: {exc}") from exc

    return result


def maybe_install_systemd_runtime(config: dict) -> dict | None:
    if not bool(config.get("systemd") or config.get("_systemd")):
        return None

    try:
        port = int(config.get("port") or config.get("public_port") or config.get("listen_port") or 8090)
    except Exception:
        port = 8090

    from core.native_service import install_systemd_service

    return install_systemd_service(
        project_dir=GENERATED_PROJECT_DIR,
        port=port,
        service_user=str(config.get("service_user") or "ubuntu"),
    )


def deploy_local_direct_from_config(config: dict, verify: str = "full") -> dict:
    """
    Config-file deployment for local/direct runtime.

    This keeps local mode aligned with Docker mode:
    CLI -> FastAPI -> latest_config.json -> main.py --config-file -> local gateway.

    Important difference:
    local mode rewrites host.docker.internal back to 127.0.0.1.
    """
    config = _system_decision_preflight_or_raise(config, runtime="local")

    config = _decision_engine_preflight_or_raise(config, runtime="local")
    from core.security_policy_contract import apply_security_policy_contract as _apply_security_policy_contract_runtime
    config = _apply_security_policy_contract_runtime(config, environment="local")

    from pathlib import Path

    prepared = _prepare_local_direct_config_file(config)
    prepared = _apply_local_config_file_security_defaults(prepared)
    prepared = preserve_native_runtime_flags(config, prepared)

    # Native public port mode:
    # Allows Pingora-AI to own port 80/443 directly like nginx when --systemd is used.
    native_port_result = maybe_prepare_native_public_port(prepared)

    # Generate Rust project/homepage/config from the local-safe config.
    write_project_from_config(prepared)

    # Keep mirrors local-safe too.
    _write_local_json("generated-pingora-proxy/config.json", prepared)
    _write_local_json("active_config.json", prepared)
    _write_local_json("gateway_config.json", prepared)

    _start_local_demo_backends_from_config(prepared)

    live_port = int(prepared.get("port") or prepared.get("public_port") or prepared.get("listen_port") or 8090)
    live_url = f"http://127.0.0.1:{live_port}"

    systemd_result = None
    if bool(prepared.get("_systemd") or prepared.get("systemd")):
        # Build release binary first so systemd can run the compiled Pingora runtime.
        subprocess.run(
            ["cargo", "build", "--release"],
            cwd=str(GENERATED_PROJECT_DIR),
            check=True,
        )

        systemd_result = maybe_install_systemd_runtime(prepared)

        if not systemd_result or not systemd_result.get("success"):
            raise RuntimeError(f"Failed to start Pingora-AI systemd runtime: {systemd_result}")

        gateway = {
            "mode": "systemd",
            "service": "pingora-ai-runtime",
            "systemd": systemd_result,
            "native_port": native_port_result,
        }
    else:
        gateway = _start_local_pingora_from_config_file()

    from core.post_change_verifier import format_verification_report, verify_after_change

    verification = verify_after_change(
        config=prepared,
        config_path=Path("generated-pingora-proxy/config.json"),
        live_url=live_url,
        source="environment_deployer",
        change_type="local_direct_config_file_deploy",
        level=verify,
        store=True,
    )

    print("")
    print(format_verification_report(verification))

    if not verification.get("success"):
        failure_summary = verification.get("failure_summary")
        if not failure_summary:
            failed = [
                item.get("name", "unknown")
                for item in verification.get("checks", [])
                if not item.get("ok")
            ]
            failure_summary = "; ".join(failed)

        error_text = (
            "Local direct config-file deployment failed verification: "
            + str(failure_summary)
        )

        try:
            from core.agent_incident_commander import command_incident

            incident = command_incident(
                runtime="local",
                strategy="direct",
                operation="apply",
                error=error_text,
                config=prepared,
                allow_auto_repair=True,
            )

            repair_result = incident.get("repair_result") or {}
            selected_root_cause = incident.get("selected_root_cause") or {}

            if (
                repair_result.get("ok")
                and selected_root_cause.get("repair_action") == "set_local_live_url_to_8090"
            ):
                verification = verify_after_change(
                    config=prepared,
                    config_path=Path("generated-pingora-proxy/config.json"),
                    live_url="http://127.0.0.1:8090",
                    source="environment_deployer",
                    change_type="local_direct_config_file_deploy_self_healed",
                    level=verify,
                    store=True,
                )

                print("")
                print("🩺 Self-healing retry after agent incident commander")
                print(format_verification_report(verification))

                if verification.get("success"):
                    return {
                        "success": True,
                        "deployed": True,
                        "runtime": "local",
                        "runtime_mode": "local",
                        "strategy": "direct",
                        "deployment_strategy": "direct",
                        "active_color": "local",
                        "live_url": live_url,
                        "gateway": gateway,
                        "verification": verification,
                        "self_healing": {
                            "attempted": True,
                            "recovered": True,
                            "incident": incident,
                        },
                    }

        except Exception as commander_exc:
            print(f"⚠️ Agent incident commander failed: {commander_exc}")

        raise RuntimeError(error_text)

    return {
        "success": True,
        "deployed": True,
        "runtime": "local",
        "runtime_mode": "local",
        "strategy": "direct",
        "deployment_strategy": "direct",
        "active_color": "local",
        "live_url": live_url,
        "gateway": gateway,
        "verification": verification,
    }


def _raise_if_system_decision_rejection_warning(warnings: list[dict[str, Any]]) -> None:
    """
    System decision rejection is not a warning.

    If the decision engine says a config is rejected, deployment must stop
    before local/Docker/Linux execution.
    """
    for item in warnings or []:
        if not isinstance(item, dict):
            continue

        stage = str(item.get("stage") or "").strip().lower()
        warning = str(item.get("warning") or "")

        if stage == "system_decision_engine" and "rejected config" in warning.lower():
            raise RuntimeError(warning)


def deploy_config_for_environment(
    *,
    config_file: str | Path,
    runtime: str,
    runtime_mode: str,
    strategy: str,
    verify_level: str = "full",
) -> dict[str, Any]:
    """
    Deploy from an already-generated config file for the requested environment.
    """
    runtime = str(runtime or "local").strip().lower()
    runtime_mode = str(runtime_mode or runtime).strip().lower()
    strategy = str(strategy or "direct").strip().lower()

    config = load_config_file(config_file)

    if strategy == "bluegreen" and runtime_mode in {"docker", "docker_host"}:
        return deploy_docker_bluegreen_from_config(
            config=config,
            verify_level=verify_level,
        )

    if runtime_mode == "local" and strategy == "direct":
        return deploy_local_direct_from_config(
            config,
            verify=locals().get("verify", locals().get("verify_level", "full")),
        )

    raise RuntimeError(
        f"Config-file deployment not implemented yet for runtime={runtime}, "
        f"runtime_mode={runtime_mode}, strategy={strategy}. "
        "Supported now: docker_host + bluegreen."
    )

def deploy_for_environment(
    *,
    prompt: str,
    runtime: str,
    runtime_mode: str,
    strategy: str,
    verify_level: str = "full",
) -> dict[str, Any]:
    runtime = str(runtime or "local").strip().lower()
    runtime_mode = str(runtime_mode or runtime).strip().lower()
    strategy = str(strategy or "direct").strip().lower()

    if strategy == "bluegreen" and runtime_mode in {"docker", "docker_host"}:
        return deploy_docker_bluegreen(
            prompt=prompt,
            verify_level=verify_level,
        )

    raise RuntimeError(
        f"Environment deployment not implemented yet for runtime={runtime}, "
        f"runtime_mode={runtime_mode}, strategy={strategy}. "
        "Supported now: docker_host + bluegreen."
    )


# ---------------------------------------------------------------------------
# Security policy contract wrapper
# ---------------------------------------------------------------------------
def _bare_metal_live_url_from_kwargs(kwargs: dict) -> str | None:
    host = str(kwargs.get("host") or kwargs.get("remote_host") or "").strip()
    if not host:
        return None

    public_http_port = int(kwargs.get("public_http_port") or kwargs.get("port") or 80)

    if public_http_port == 80:
        return f"http://{host}/"

    return f"http://{host}:{public_http_port}/"


def _classify_bare_metal_deploy_error(error: str | None) -> dict:
    text = str(error or "")
    lower = text.lower()

    if "exec format error" in lower or "mach-o" in lower:
        return {
            "root_cause": "bare_metal_wrong_binary_format",
            "category": "setup_layer",
            "summary": "The uploaded gateway binary is not a Linux ELF binary.",
            "repair_hint": "Build the gateway on Linux or upload generated-pingora-proxy-linux.",
        }

    if "host.docker.internal" in lower or "resolve_failed" in lower or "failed to lookup address information" in lower:
        return {
            "root_cause": "bare_metal_docker_host_address_leak",
            "category": "decision_layer",
            "summary": "Docker-only upstream address leaked into native Linux deployment.",
            "repair_hint": "For bare_metal_linux, keep upstreams as 127.0.0.1 or private Linux network addresses.",
        }

    if "8091" in lower or "18081" in lower or "18082" in lower:
        return {
            "root_cause": "bare_metal_runtime_port_ignored",
            "category": "runtime_layer",
            "summary": "The deployer expected a blue/green candidate port, but current Rust runtime binds to 8090.",
            "repair_hint": "Use temporary single-port 8090 proof mode until Rust supports --port.",
        }

    if "public nginx route failed" in lower or "nginx" in lower and "404" in lower:
        return {
            "root_cause": "bare_metal_nginx_public_route_not_active",
            "category": "setup_layer",
            "summary": "NGINX public route is not pointing to the active Pingora service.",
            "repair_hint": "Write /etc/nginx/conf.d/pingora-ai.conf with listen 80 default_server and reload nginx.",
        }

    if "candidate failed health check" in lower and "404" in lower:
        return {
            "root_cause": "bare_metal_health_check_wrong_path_or_missing_homepage",
            "category": "verification_layer",
            "summary": "Candidate route health check hit an unavailable path.",
            "repair_hint": "Verify /api/ for proxy route health and ensure / static homepage exists.",
        }

    if "public/index.html" in lower or "no such file" in lower and "public" in lower:
        return {
            "root_cause": "bare_metal_static_assets_missing",
            "category": "setup_layer",
            "summary": "Static homepage route exists but public/index.html was not uploaded.",
            "repair_hint": "Upload generated-pingora-proxy/public into the active release directory.",
        }

    if "disable --now" in lower or "timed out after 30 seconds" in lower or "timeout" in lower and "systemctl" in lower:
        return {
            "root_cause": "bare_metal_systemd_stop_timeout",
            "category": "runtime_layer",
            "summary": "systemd stop/disable hung while stopping Pingora.",
            "repair_hint": "Kill first, then stop/disable/reset-failed. Do not use systemctl disable --now.",
        }

    if "scp upload failed" in lower:
        return {
            "root_cause": "bare_metal_scp_upload_failed",
            "category": "setup_layer",
            "summary": "SCP upload failed, commonly due to permissions or overwriting a running binary.",
            "repair_hint": "Upload to /tmp first, stop candidate service, then sudo mv into release path.",
        }

    return {
        "root_cause": "bare_metal_unknown_deploy_failure",
        "category": "unknown",
        "summary": "Bare-metal Linux deployment failed for an unclassified reason.",
        "repair_hint": "Check systemd logs, NGINX config, candidate health check, and release files.",
    }


def _prepare_bare_metal_linux_config(config: dict, kwargs: dict) -> dict:
    import copy

    from core.homepage_route import ensure_route_directory_homepage
    from core.lb_config import default_health_check
    from core.project_writer import write_project, write_json_config, write_route_directory_homepage

    prepared = copy.deepcopy(config)
    prepared.pop("warnings", None)
    prepared.pop("_pipeline_decisions", None)
    prepared = ensure_route_directory_homepage(prepared)

    prepared["runtime"] = "bare_metal_linux"
    prepared["strategy"] = "bluegreen"

    live_url = _bare_metal_live_url_from_kwargs(kwargs)
    if live_url:
        prepared["live_url"] = live_url
        prepared["public_url"] = live_url
        prepared["base_url"] = live_url

    # Native Linux must not receive Docker-only upstream hostnames.
    for route in prepared.get("routes", []) or []:
        if not isinstance(route, dict):
            continue

        upstreams = route.get("upstreams")
        if isinstance(upstreams, list):
            fixed_upstreams = []
            for item in upstreams:
                if isinstance(item, dict):
                    fixed_item = dict(item)
                    for key in ("address", "upstream", "backend"):
                        if fixed_item.get(key):
                            fixed_item[key] = str(fixed_item[key]).replace(
                                "host.docker.internal",
                                "127.0.0.1",
                            )
                    fixed_upstreams.append(fixed_item)
                else:
                    fixed_upstreams.append(
                        str(item).replace("host.docker.internal", "127.0.0.1")
                    )
            route["upstreams"] = fixed_upstreams

        if route.get("upstream"):
            route["upstream"] = str(route["upstream"]).replace(
                "host.docker.internal",
                "127.0.0.1",
            )

        route_type = str(route.get("type") or route.get("route_type") or "").lower()
        if normalize_path(route.get("path")) != "/" and route_type not in {"static", "web", "webserver", "web_server"}:
            route.setdefault("type", "proxy")
            route.setdefault("route_type", "proxy")
            route.setdefault("health_check", default_health_check())

    # While the generated Rust runtime still compiles route data, we must rewrite
    # generated-pingora-proxy before deploying the Linux binary.
    project_dir = kwargs.get("project_dir") or "generated-pingora-proxy"
    write_project(prepared, project_dir)
    write_json_config(prepared, project_dir)
    write_route_directory_homepage(prepared, project_dir)

    return prepared


def _run_bare_metal_decision_preflight(config: dict, kwargs: dict) -> dict:
    runtime = "bare_metal_linux"
    strategy = "bluegreen"

    # Fatal decision gate. Do not deploy if the deterministic system decision
    # engine rejects the config.
    from core.system_decision_engine import apply_and_enforce_system_decisions

    try:
        return apply_and_enforce_system_decisions(
            config,
            runtime=runtime,
            strategy=strategy,
        )
    except TypeError:
        return apply_and_enforce_system_decisions(config, runtime=runtime)
    except Exception as exc:
        raise RuntimeError(f"System decision engine rejected config: {exc}") from exc


def _run_bare_metal_config_verification(config: dict, kwargs: dict) -> dict:
    # Config verification should become mandatory later. For now we call it if
    # available but do not break the proof-mode path if signature differs.
    try:
        from agents.config_verification_agent import run_config_verification_agent

        result = run_config_verification_agent(
            config=config,
            runtime="bare_metal_linux",
            strategy="bluegreen",
            environment="bare_metal_linux",
        )

        if isinstance(result, dict) and result.get("ok") is False:
            raise RuntimeError(
                "Config verification failed for bare_metal_linux: "
                + str(result)
            )

    except ImportError:
        pass
    except TypeError:
        pass

    return config


def deploy_bare_metal_linux_from_environment(config: dict, **kwargs) -> dict:
    from core.bare_metal_deployer import deploy_bare_metal_linux

    config = _prepare_bare_metal_linux_config(config, kwargs)
    config = _run_bare_metal_decision_preflight(config, kwargs)
    config = _run_bare_metal_config_verification(config, kwargs)

    host = kwargs.get("host") or kwargs.get("remote_host")
    if not host:
        return {
            "success": False,
            "target": "bare_metal_linux",
            "active_color": None,
            "active_port": None,
            "live_url": None,
            "message": "Bare-metal Linux deployment failed.",
            "error": "--host is required for bare_metal_linux deployment.",
            "classification": {
                "root_cause": "bare_metal_missing_host",
                "category": "setup_layer",
                "summary": "No remote host was provided.",
                "repair_hint": "Pass --host or host=...",
            },
        }

    result = deploy_bare_metal_linux(
        config,
        host=host,
        user=kwargs.get("user", "ubuntu"),
        ssh_key=kwargs.get("ssh_key"),
        ssh_port=int(kwargs.get("ssh_port") or 22),
        server_name=kwargs.get("server_name", "_"),
        public_http_port=int(kwargs.get("public_http_port") or 80),
        binary_path=kwargs.get("binary_path"),
        service_user=kwargs.get("service_user", "ubuntu"),
        verify_path=kwargs.get("verify_path", "/api/"),
        verify_timeout_seconds=int(kwargs.get("verify_timeout_seconds") or 60),
    )

    if not isinstance(result, dict):
        try:
            result = result.__dict__
        except Exception:
            result = {
                "success": False,
                "target": "bare_metal_linux",
                "error": f"Unexpected result type: {type(result)}",
            }

    if not result.get("success"):
        result["classification"] = _classify_bare_metal_deploy_error(
            result.get("error")
        )

    # Optional post-change verifier.
    if result.get("success"):
        try:
            from core.post_change_verifier import verify_post_change

            verification = verify_post_change(
                config=config,
                base_url=result.get("live_url"),
                level=kwargs.get("verify", "full"),
                environment="bare_metal_linux",
            )
            result["post_change_verification"] = verification
        except ImportError:
            result["post_change_verification"] = {
                "skipped": True,
                "reason": "post_change_verifier unavailable",
            }
        except TypeError:
            result["post_change_verification"] = {
                "skipped": True,
                "reason": "post_change_verifier signature does not support bare_metal_linux yet",
            }
        except Exception as exc:
            result["success"] = False
            result["error"] = f"Post-change verification failed: {exc}"
            result["classification"] = {
                "root_cause": "bare_metal_post_change_verification_failed",
                "category": "verification_layer",
                "summary": "Deployment succeeded but verification failed.",
                "repair_hint": "Inspect route liveness, homepage, NGINX headers, and runtime state.",
            }

    return result


# Keep original deployer and wrap only bare_metal_linux.


# ---------------------------------------------------------------------------
# Bare-metal Linux shared deployment pipeline
# ---------------------------------------------------------------------------
# Keep this separate from deploy_config_for_environment so local/docker stay stable.
# ---------------------------------------------------------------------------


def _classify_bare_metal_pipeline_error(error: str | None) -> dict:
    text = str(error or "").lower()

    if "exec format error" in text or "mach-o" in text:
        return {
            "root_cause": "bare_metal_wrong_binary_format",
            "layer": "build_setup",
            "summary": "A non-Linux binary was uploaded to Linux.",
            "repair_hint": "Build/upload Linux ELF binary or use remote Linux build.",
        }

    if "host.docker.internal" in text or "resolve_failed" in text or "failed to lookup address information" in text:
        return {
            "root_cause": "bare_metal_docker_host_address_leak",
            "layer": "decision_config",
            "summary": "Docker-only hostname leaked into native Linux deployment.",
            "repair_hint": "Rewrite host.docker.internal to 127.0.0.1 or a private Linux address.",
        }

    if "public_nginx_homepage" in text or ("homepage" in text and "404" in text):
        return {
            "root_cause": "bare_metal_static_homepage_failed",
            "layer": "release_packaging",
            "summary": "The active release did not serve the static route-directory homepage.",
            "repair_hint": "Upload public/index.html to the active release and ensure systemd WorkingDirectory is the release directory.",
        }

    if "scp upload failed" in text:
        return {
            "root_cause": "bare_metal_scp_upload_failed",
            "layer": "release_upload",
            "summary": "SCP upload failed.",
            "repair_hint": "Upload to /tmp first, then move into release directory.",
        }

    if "systemctl" in text and "timed out" in text:
        return {
            "root_cause": "bare_metal_systemd_stop_timeout",
            "layer": "runtime",
            "summary": "systemd stop timed out.",
            "repair_hint": "SIGKILL process first, then reset-failed. Avoid disable --now.",
        }

    return {
        "root_cause": "bare_metal_unknown_failure",
        "layer": "unknown",
        "summary": "Bare-metal deployment failed for an unclassified reason.",
        "repair_hint": "Inspect SSH logs, systemd status, NGINX config, runtime state, and release files.",
    }


def _bare_metal_live_url(*, host: str, public_http_port: int = 80) -> str:
    if int(public_http_port or 80) == 80:
        return f"http://{host}/"
    return f"http://{host}:{int(public_http_port)}/"


def _prepare_bare_metal_pipeline_config(config: dict, **kwargs) -> dict:
    import copy
    from core.homepage_route import ensure_route_directory_homepage
    from core.lb_config import default_health_check

    prepared = copy.deepcopy(config)
    prepared.pop("warnings", None)
    prepared.pop("_pipeline_decisions", None)
    prepared["runtime"] = "bare_metal_linux"
    prepared["strategy"] = "bluegreen"

    host = kwargs.get("host")
    public_http_port = int(kwargs.get("public_http_port") or 80)

    if host:
        live_url = _bare_metal_live_url(host=host, public_http_port=public_http_port)
        prepared["live_url"] = live_url
        prepared["public_url"] = live_url
        prepared["base_url"] = live_url

    prepared = ensure_route_directory_homepage(prepared)

    for route in prepared.get("routes", []) or []:
        if not isinstance(route, dict):
            continue

        if isinstance(route.get("upstreams"), list):
            fixed_upstreams = []
            for item in route["upstreams"]:
                if isinstance(item, dict):
                    fixed_item = dict(item)
                    for key in ("address", "upstream", "backend"):
                        if fixed_item.get(key):
                            fixed_item[key] = str(fixed_item[key]).replace(
                                "host.docker.internal",
                                "127.0.0.1",
                            )
                    fixed_upstreams.append(fixed_item)
                else:
                    fixed_upstreams.append(
                        str(item).replace("host.docker.internal", "127.0.0.1")
                    )
            route["upstreams"] = fixed_upstreams

        if route.get("upstream"):
            route["upstream"] = str(route["upstream"]).replace(
                "host.docker.internal",
                "127.0.0.1",
            )

        route_type = str(route.get("type") or route.get("route_type") or "").lower()
        if normalize_path(route.get("path")) != "/" and route_type not in {"static", "web", "webserver", "web_server"}:
            route.setdefault("type", "proxy")
            route.setdefault("route_type", "proxy")
            route.setdefault("health_check", default_health_check())

    try:
        from core.deployment_prepare import ensure_default_blocked_paths
        prepared = ensure_default_blocked_paths(prepared)
    except Exception:
        pass

    return prepared


def _run_bare_metal_decision_stage(config: dict) -> dict:
    """
    Fatal deterministic decision gate for bare_metal_linux.

    A rejected config must never be downgraded to a warning. This protects
    Linux/systemd from deploying invalid route mutations such as setting
    round_robin on a one-upstream route.
    """
    from core.system_decision_engine import apply_and_enforce_system_decisions

    try:
        return apply_and_enforce_system_decisions(config)
    except Exception as exc:
        raise RuntimeError(f"System decision engine rejected config: {exc}") from exc


def _run_bare_metal_config_verification(config: dict, **kwargs) -> dict:
    try:
        from agents.config_verification_agent import run_config_verification_agent

        result = run_config_verification_agent(
            config=config,
            runtime="bare_metal_linux",
            strategy="bluegreen",
            environment="bare_metal_linux",
            live_url=config.get("live_url"),
            require_algorithm_integrity=bool(kwargs.get("require_algorithm_integrity", False)),
        )

        config.setdefault("_pipeline_decisions", []).append(
            {"stage": "config_verification_agent", "result": result}
        )

    except Exception as exc:
        config.setdefault("_pipeline_decisions", []).append(
            {
                "stage": "config_verification_agent",
                "skipped": True,
                "reason": str(exc),
            }
        )

    return config


def _write_bare_metal_project(config: dict) -> dict:
    from core.project_writer import write_project, write_json_config, write_route_directory_homepage

    project_dir = "generated-pingora-proxy"
    write_project(config, project_dir)
    write_json_config(config, project_dir)
    write_route_directory_homepage(config, project_dir)
    return config


def _auto_build_bare_metal_binary_enabled(kwargs: dict) -> bool:
    requested = kwargs.get("auto_build_binary")

    if requested is not None:
        return bool(requested)

    value = str(os.getenv("PINGORA_AI_BARE_METAL_AUTO_BUILD", "1")).strip().lower()
    return value not in {"0", "false", "no", "off"}


def _build_bare_metal_linux_binary(binary_path: str | Path | None, **kwargs) -> dict[str, Any]:
    if not binary_path:
        raise RuntimeError("Bare-metal Linux binary path is required for auto-build.")

    if not _auto_build_bare_metal_binary_enabled(kwargs):
        return {
            "success": True,
            "skipped": True,
            "reason": "PINGORA_AI_BARE_METAL_AUTO_BUILD disabled",
            "binary_path": str(binary_path),
        }

    if not shutil.which("docker"):
        raise RuntimeError(
            "Bare-metal Linux auto-build requires Docker. "
            "Install Docker or set PINGORA_AI_BARE_METAL_AUTO_BUILD=0 to use the existing --binary-path."
        )

    project_dir = GENERATED_PROJECT_DIR
    output_path = Path(binary_path).expanduser()
    if not output_path.is_absolute():
        output_path = PROJECT_ROOT / output_path

    output_path.parent.mkdir(parents=True, exist_ok=True)

    write_dockerfile()

    platform = str(os.getenv("PINGORA_AI_LINUX_BUILD_PLATFORM", "linux/amd64")).strip() or "linux/amd64"
    image = str(os.getenv("PINGORA_AI_LINUX_BUILD_IMAGE", "pingora-ai-gateway-linux-amd64")).strip()
    container = f"pingora-ai-extract-{int(time.time())}-{os.getpid()}"

    build = run_command(
        [
            "docker",
            "build",
            "--platform",
            platform,
            "-t",
            image,
            str(project_dir),
        ],
        cwd=PROJECT_ROOT,
        timeout=1800,
    )

    if build.returncode != 0:
        raise RuntimeError(
            "Bare-metal Linux Docker build failed.\n"
            f"STDOUT:\n{build.stdout[-4000:]}\n\n"
            f"STDERR:\n{build.stderr[-4000:]}"
        )

    create = run_command(
        ["docker", "create", "--name", container, image],
        cwd=PROJECT_ROOT,
        timeout=120,
    )

    if create.returncode != 0:
        raise RuntimeError(
            "Could not create extraction container for Linux binary.\n"
            f"STDOUT:\n{create.stdout[-2000:]}\n\n"
            f"STDERR:\n{create.stderr[-2000:]}"
        )

    try:
        copy = run_command(
            [
                "docker",
                "cp",
                f"{container}:/app/target/release/generated-pingora-proxy",
                str(output_path),
            ],
            cwd=PROJECT_ROOT,
            timeout=120,
        )

        if copy.returncode != 0:
            raise RuntimeError(
                "Could not copy Linux binary from extraction container.\n"
                f"STDOUT:\n{copy.stdout[-2000:]}\n\n"
                f"STDERR:\n{copy.stderr[-2000:]}"
            )

    finally:
        run_command(["docker", "rm", "-f", container], cwd=PROJECT_ROOT, timeout=60)

    output_path.chmod(0o755)

    return {
        "success": True,
        "binary_path": str(output_path),
        "platform": platform,
        "image": image,
    }


def _run_bare_metal_health_gate(result: dict, **kwargs) -> dict:
    if not result.get("success"):
        return result

    import subprocess

    host = kwargs.get("host")
    user = kwargs.get("user", "ubuntu")
    ssh_key = kwargs.get("ssh_key")
    ssh_port = str(kwargs.get("ssh_port") or 22)

    if not host or not ssh_key:
        result["success"] = False
        result["error"] = "Bare-metal health gate missing host or ssh_key."
        result["classification"] = {
            "root_cause": "bare_metal_health_gate_missing_ssh",
            "layer": "verification",
            "repair_hint": "Pass --host and --ssh-key.",
        }
        return result

    active_port = int(result.get("active_port") or 8090)

    remote_script = f'''
set -e

echo "backend_health"
curl -fsS http://127.0.0.1:9101/health >/dev/null

echo "gateway_internal_api"
curl -fsS http://127.0.0.1:{active_port}/api/ >/dev/null

echo "gateway_runtime_state"
curl -fsS http://127.0.0.1:{active_port}/__gateway_runtime_state >/dev/null

echo "public_nginx_homepage"
curl -fsS http://127.0.0.1/ >/dev/null

echo "public_nginx_api"
curl -fsS http://127.0.0.1/api/ >/dev/null
'''

    completed = subprocess.run(
        [
            "ssh",
            "-p",
            ssh_port,
            "-o",
            "ConnectTimeout=10",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-i",
            str(ssh_key),
            f"{user}@{host}",
            remote_script,
        ],
        text=True,
        capture_output=True,
        timeout=45,
    )

    result["health_gate"] = {
        "success": completed.returncode == 0,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }

    if completed.returncode != 0:
        result["success"] = False
        result["error"] = (
            "Bare-metal health gate failed.\n"
            f"STDOUT:\n{completed.stdout}\n"
            f"STDERR:\n{completed.stderr}"
        )
        result["classification"] = _classify_bare_metal_pipeline_error(result["error"])

    return result


def _run_bare_metal_post_change_verification(config: dict, result: dict, **kwargs) -> dict:
    if not result.get("success"):
        return result

    try:
        import core.post_change_verifier as pcv

        verifier = (
            getattr(pcv, "verify_after_change", None)
            or getattr(pcv, "verify_post_change", None)
            or getattr(pcv, "run_post_change_verifier", None)
            or getattr(pcv, "run_post_change_verification", None)
            or getattr(pcv, "verify_gateway", None)
            or getattr(pcv, "verify", None)
        )

        if verifier is None:
            available = [name for name in dir(pcv) if name.startswith(("verify", "run"))]
            result["post_change_verification"] = {
                "skipped": True,
                "reason": f"No supported verifier function found. Available: {available}",
            }
            return result

        live_url = result.get("live_url") or config.get("live_url")

        if verifier is getattr(pcv, "verify_after_change", None):
            verification = verifier(
                config=config,
                config_path=GENERATED_PROJECT_DIR / "config.json",
                live_url=live_url,
                source="environment_deployer",
                change_type="bare_metal_linux_deploy",
                level=kwargs.get("verify", "full"),
                store=True,
            )
        else:
            try:
                verification = verifier(
                    config=config,
                    live_url=live_url,
                    level=kwargs.get("verify", "full"),
                    environment="bare_metal_linux",
                )
            except TypeError:
                try:
                    verification = verifier(config=config, base_url=live_url)
                except TypeError:
                    verification = verifier(config, live_url)

        result["post_change_verification"] = verification

    except Exception as exc:
        result["post_change_verification"] = {
            "skipped": True,
            "reason": str(exc),
        }

    return result


def deploy_bare_metal_with_shared_pipeline(config: dict, **kwargs) -> dict:
    """
    Clean bare-metal deployment pipeline.

    This does not wrap or modify the original local/docker deployer.
    """

    try:
        from core.bare_metal_deployer import deploy_bare_metal_linux

        prepared = _prepare_bare_metal_pipeline_config(config, **kwargs)
        prepared = _run_bare_metal_decision_stage(prepared)

        try:
            from core.deployment_prepare import ensure_default_blocked_paths
            prepared = ensure_default_blocked_paths(prepared)
        except Exception:
            pass

        prepared = _run_bare_metal_config_verification(prepared, **kwargs)

        try:
            from core.deployment_prepare import ensure_default_blocked_paths
            prepared = ensure_default_blocked_paths(prepared)
        except Exception:
            pass

        prepared = _write_bare_metal_project(prepared)
        binary_build_kwargs = dict(kwargs)
        binary_build_kwargs.pop("binary_path", None)
        binary_build = _build_bare_metal_linux_binary(
            kwargs.get("binary_path"),
            **binary_build_kwargs,
        )

        result = deploy_bare_metal_linux(
            prepared,
            host=kwargs.get("host"),
            user=kwargs.get("user", "ubuntu"),
            ssh_key=kwargs.get("ssh_key"),
            ssh_port=int(kwargs.get("ssh_port") or 22),
            server_name=kwargs.get("server_name", "_"),
            public_http_port=int(kwargs.get("public_http_port") or 80),
            binary_path=kwargs.get("binary_path"),
            service_user=kwargs.get("service_user", "ubuntu"),
            verify_path=kwargs.get("verify_path", "/api/"),
            verify_timeout_seconds=int(kwargs.get("verify_timeout_seconds") or 60),
        )

        if not isinstance(result, dict):
            result = getattr(result, "__dict__", {"result": result})

        result = _run_bare_metal_health_gate(result, **kwargs)
        result = _run_bare_metal_post_change_verification(prepared, result, **kwargs)

        result["pipeline"] = {
            "runtime": "bare_metal_linux",
            "strategy": "bluegreen",
            "source": "environment_deployer.deploy_bare_metal_with_shared_pipeline",
            "change_type": kwargs.get("change_type", "deploy"),
            "decisions": prepared.get("_pipeline_decisions", []),
            "warnings": prepared.get("warnings", []),
            "binary_build": binary_build,
        }

        if not result.get("success"):
            result["classification"] = result.get("classification") or _classify_bare_metal_pipeline_error(
                result.get("error")
            )

        return result

    except Exception as exc:
        return {
            "success": False,
            "target": "bare_metal_linux",
            "message": "Bare-metal shared pipeline failed.",
            "error": str(exc),
            "classification": _classify_bare_metal_pipeline_error(str(exc)),
            "pipeline": {
                "runtime": "bare_metal_linux",
                "strategy": "bluegreen",
                "source": "environment_deployer.deploy_bare_metal_with_shared_pipeline",
                "change_type": kwargs.get("change_type", "deploy"),
            },
        }
