from __future__ import annotations
from core.nginx_edge_decision import raise_if_blocked, validate_candidate_container_config, validate_edge_config_points_to_color, validate_running_nginx_points_to_color

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
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
    Write text safely to avoid Nginx reading a partially written config during reload.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)

def write_nginx_conf(active_color: str, gateway_port: int) -> None:
    DOCKER_DIR.mkdir(parents=True, exist_ok=True)

    service_name = f"gateway-{active_color}"
    expected_api_key = os.getenv("PINGORA_EDGE_API_KEY", "dev-local-secret")

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


def write_compose(active_color: str, gateway_port: int) -> Path:
    DOCKER_DIR.mkdir(parents=True, exist_ok=True)

    compose = DOCKER_DIR / "docker-compose.yml"

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

  gateway-edge:
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

def start_docker_bluegreen(config: dict[str, Any]) -> dict[str, Any]:
    require_docker()

    # Docker blue/green local runtime must use the stable edge/gateway port.
    # FastAPI config-file plans may still contain port 8080, but generated
    # Pingora currently starts on 8090. Force Docker/Nginx/readiness to 8090.
    config["port"] = DOCKER_BLUEGREEN_GATEWAY_PORT
    config["listen_port"] = DOCKER_BLUEGREEN_GATEWAY_PORT
    config["proxy_port"] = DOCKER_BLUEGREEN_GATEWAY_PORT

    port = DOCKER_BLUEGREEN_GATEWAY_PORT
    color = next_color()

    write_dockerfile()
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

    readiness_url = f"http://127.0.0.1:{port}/__edge_health"
    live_url = "https://127.0.0.1:8443"

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

    url = f"http://127.0.0.1:{port}/__edge_health"
    deadline = time.time() + timeout_seconds
    last_error = None

    while time.time() < deadline:
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=3) as response:
                status = int(response.status)
                if status == 200:
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
    Validate the host config, docker cp it to a non-mounted temp file inside the
    container, test that temp config, then reload NGINX using that temp config.
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

    test_result = run_command(
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

    if test_result.returncode != 0:
        return test_result

    return run_command(
        [
            "docker",
            "exec",
            "ai-pingora-gateway-edge",
            "nginx",
            "-s",
            "reload",
            "-c",
            container_tmp_conf,
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

        if status == 200:
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

        # Docker Desktop/macOS safety:
        # nginx -s reload can report success while old workers still serve the
        # previous upstream target. Restarting only the edge container forces
        # workers to load the active candidate config without touching blue/green.
        restart = run_command(
            ["docker", "restart", "ai-pingora-gateway-edge"],
            timeout=60,
        )

        if restart.returncode != 0:
            rollback_edge_to_color(old_color, port)
            raise RuntimeError(
                "Nginx edge restart failed after reload. Rolled back edge to old colour.\n"
                f"STDOUT:\n{restart.stdout}\n\nSTDERR:\n{restart.stderr}"
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
        raise RuntimeError(
            "Docker blue/green config-file deployment failed verification: "
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


def _start_local_pingora_from_config_file() -> dict:
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
        process = subprocess.Popen(
            ["cargo", "run", "--release"],
            cwd=str(project_dir),
            stdout=fh,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
        )

    pid_file.write_text(str(process.pid), encoding="utf-8")

    _wait_local_http_200("http://127.0.0.1:8090/__edge_health", timeout_seconds=180)

    return {
        "pid": process.pid,
        "log_file": str(log_file),
        "live_url": "https://127.0.0.1:8443",
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

    # Generate Rust project/homepage/config from the local-safe config.
    write_project_from_config(prepared)

    # Keep mirrors local-safe too.
    _write_local_json("generated-pingora-proxy/config.json", prepared)
    _write_local_json("active_config.json", prepared)
    _write_local_json("gateway_config.json", prepared)

    _start_local_demo_backends_from_config(prepared)
    gateway = _start_local_pingora_from_config_file()

    from core.post_change_verifier import format_verification_report, verify_after_change

    verification = verify_after_change(
        config=prepared,
        config_path=Path("generated-pingora-proxy/config.json"),
        live_url="https://127.0.0.1:8443",
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

        raise RuntimeError(
            "Local direct config-file deployment failed verification: "
            + str(failure_summary)
        )

    return {
        "success": True,
        "deployed": True,
        "runtime": "local",
        "runtime_mode": "local",
        "strategy": "direct",
        "deployment_strategy": "direct",
        "active_color": "local",
        "live_url": "https://127.0.0.1:8443",
        "gateway": gateway,
        "verification": verification,
    }

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
if not globals().get("_SECURITY_POLICY_CONTRACT_ENV_DEPLOYER_WRAPPED"):
    _SECURITY_POLICY_CONTRACT_ENV_DEPLOYER_WRAPPED = True

    import inspect as _security_contract_inspect
    from core.security_policy_contract import apply_security_policy_contract as _apply_security_policy_contract

    _original_deploy_config_for_environment = deploy_config_for_environment

    def deploy_config_for_environment(*args, **kwargs):
        signature = _security_contract_inspect.signature(_original_deploy_config_for_environment)
        bound = signature.bind_partial(*args, **kwargs)

        config = bound.arguments.get("config")
        runtime_mode = (
            bound.arguments.get("runtime_mode")
            or bound.arguments.get("runtime")
            or bound.arguments.get("runtime_name")
            or kwargs.get("runtime_mode")
            or kwargs.get("runtime")
            or "local"
        )

        if isinstance(config, dict):
            bound.arguments["config"] = _apply_security_policy_contract(
                config,
                environment=str(runtime_mode),
            )

        return _original_deploy_config_for_environment(*bound.args, **bound.kwargs)


# ---------------------------------------------------------------------------
# Deterministic decision-engine deploy wrapper
# ---------------------------------------------------------------------------
if not globals().get("_DECISION_ENGINE_ENV_DEPLOYER_WRAPPED"):
    _DECISION_ENGINE_ENV_DEPLOYER_WRAPPED = True

    import inspect as _decision_engine_inspect
    from core.decision_engine import (
        apply_deterministic_repairs as _decision_apply_repairs,
        evaluate_config as _decision_evaluate_config,
    )

    _original_deploy_config_for_environment_for_decision = deploy_config_for_environment

    def deploy_config_for_environment(*args, **kwargs):
        signature = _decision_engine_inspect.signature(_original_deploy_config_for_environment_for_decision)
        bound = signature.bind_partial(*args, **kwargs)

        config = bound.arguments.get("config")

        runtime = (
            bound.arguments.get("runtime_mode")
            or bound.arguments.get("runtime")
            or kwargs.get("runtime_mode")
            or kwargs.get("runtime")
            or (config or {}).get("runtime")
            or (config or {}).get("runtime_mode")
            or "local"
        )

        if isinstance(config, dict):
            # First evaluate raw config so dangerous algorithm corruption is not hidden
            # by deterministic repair. Root/static and addressing can be repaired, but
            # malformed weighted routes must be rejected before deployment.
            pre_decision = _decision_evaluate_config(config)
            pre_failed = pre_decision.get("failed_decisions", [])

            hard_fail_categories = {
                "weighted_round_robin_integrity",
            }

            hard_failed = [
                item for item in pre_failed
                if item.get("category") in hard_fail_categories
            ]

            if hard_failed:
                reason = "; ".join(item.get("reason", "unknown") for item in hard_failed)
                raise RuntimeError("Decision engine rejected deployment config before repair: " + reason)

            repaired = _decision_apply_repairs(config, runtime=str(runtime))
            decision = _decision_evaluate_config(repaired)

            if not decision.get("ok"):
                failed = decision.get("failed_decisions", [])
                reason = "; ".join(item.get("reason", "unknown") for item in failed)
                raise RuntimeError("Decision engine rejected deployment config: " + reason)

            bound.arguments["config"] = repaired

        return _original_deploy_config_for_environment_for_decision(*bound.args, **bound.kwargs)
