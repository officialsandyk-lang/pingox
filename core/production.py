"""Strict production configuration and reproducible deployment generation.

Legacy natural-language repair is intentionally not called here: malformed
operator intent must fail rather than silently becoming localhost:3000.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def checked_path(value: str) -> str:
    if not re.fullmatch(r"/[A-Za-z0-9_./-]*", value) or "//" in value:
        raise ValueError("path must be an absolute unencoded URL path")
    if any(part in {".", ".."} for part in value.split("/")):
        raise ValueError("dot segments are not allowed")
    if value != "/" and value.endswith("/"):
        raise ValueError("omit trailing slash from route paths")
    return value


class Upstream(StrictModel):
    address: str
    weight: int = Field(default=1, ge=1, le=1000)

    @field_validator("address")
    @classmethod
    def valid_address(cls, value: str) -> str:
        match = re.fullmatch(r"([A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?):([0-9]{1,5})", value)
        if not match or not 1 <= int(match[2]) <= 65535:
            raise ValueError("upstream must be an HTTP host:port on a trusted private network; schemes and paths are unsupported")
        if match[1] in {"0.0.0.0", "localhost", "127.0.0.1"}:
            raise ValueError("use a reachable service DNS name or private IP, not container loopback")
        return value


class Route(StrictModel):
    path: str
    upstreams: list[Upstream] = Field(min_length=1, max_length=128)
    balancing: Literal["round_robin", "weighted_round_robin", "random", "least_connections", "ip_hash"] = "round_robin"

    _path = field_validator("path")(checked_path)

    @model_validator(mode="after")
    def check_pool(self):
        if len({u.address for u in self.upstreams}) != len(self.upstreams):
            raise ValueError("duplicate upstream")
        if self.balancing != "weighted_round_robin" and any(u.weight != 1 for u in self.upstreams):
            raise ValueError("weights require weighted_round_robin")
        return self


class Security(StrictModel):
    blocked_paths: list[str] = Field(default_factory=lambda: ["/.env", "/.git"])
    allowed_methods: list[Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]] = Field(default_factory=lambda: ["GET", "HEAD", "POST"] , min_length=1)
    rate_limit_per_minute: int = Field(default=600, ge=0, le=10000000)
    max_connections: int = Field(default=1000, ge=1, le=100000)
    max_request_body_bytes: int = Field(default=1048576, ge=1, le=1073741824)
    upstream_timeout_seconds: int = Field(default=30, ge=1, le=300)

    @field_validator("blocked_paths")
    @classmethod
    def check_paths(cls, values):
        return [checked_path(v) for v in values]


class ProductionConfig(StrictModel):
    port: int = Field(default=8080, ge=1024, le=65535)
    routes: list[Route] = Field(min_length=1, max_length=1024)
    security: Security = Field(default_factory=Security)

    @model_validator(mode="after")
    def check_routes(self):
        if len({r.path for r in self.routes}) != len(self.routes):
            raise ValueError("duplicate route")
        if any(r.path == "/_gateway" or r.path.startswith("/_gateway/") for r in self.routes):
            raise ValueError("/_gateway is reserved for gateway health checks")
        return self


def generate(config: ProductionConfig, output: Path) -> Path:
    from core.project_writer import write_project
    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("output must be empty; create a new release directory")
    canonical = config.model_dump()
    runtime = {**canonical, "demo_backend": {"enabled": False, "generate_placeholder_files": False}}
    write_project(runtime, output)
    shutil.copyfile(Path(__file__).resolve().parents[1] / "deploy" / "Cargo.lock", output / "Cargo.lock")
    (output / "production.json").write_text(json.dumps(canonical, indent=2) + "\n")
    # Copy only explicit build inputs. Credentials, runtime reports and the AI
    # control plane never enter the gateway image.
    (output / "Dockerfile").write_text('''FROM rust:1.95-bookworm AS build
RUN apt-get update && apt-get install -y --no-install-recommends cmake pkg-config libssl-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY Cargo.toml Cargo.lock ./
COPY src ./src
RUN cargo build --locked --release

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libssl3 curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /build/target/release/generated-pingora-proxy /usr/local/bin/gateway
USER 65532:65532
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/local/bin/gateway"]
''')
    (output / ".dockerignore").write_text("*\n!Cargo.toml\n!Cargo.lock\n!src/\n!src/**\n!Dockerfile\n")
    (output / "compose.yaml").write_text(f'''services:
  gateway:
    image: ${{GATEWAY_IMAGE:?Set GATEWAY_IMAGE to an immutable release tag or digest}}
    build: .
    restart: unless-stopped
    read_only: true
    user: "65532:65532"
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    pids_limit: 256
    mem_limit: 512m
    cpus: "2.0"
    ulimits:
      nofile:
        soft: 65536
        hard: 65536
    stop_grace_period: 45s
    environment:
      TRUSTED_PROXY_CIDRS: ${{GATEWAY_NETWORK_CIDR:?Set a dedicated edge network CIDR}}
    networks: [gateway, upstreams]
    healthcheck:
      test: ["CMD", "curl", "--fail", "--silent", "--max-time", "2", "http://127.0.0.1:{config.port}/_gateway/health"]
      interval: 10s
      timeout: 3s
      retries: 3
      start_period: 15s
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
  edge:
    image: ${{CADDY_IMAGE:?Set CADDY_IMAGE to an approved Caddy 2 image digest}}
    restart: unless-stopped
    ports: ["80:80", "443:443"]
    environment:
      GATEWAY_DOMAIN: ${{GATEWAY_DOMAIN:?Set the public DNS hostname}}
      ACME_EMAIL: ${{ACME_EMAIL:?Set the certificate contact email}}
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    networks: [gateway]
    depends_on:
      gateway:
        condition: service_healthy
    security_opt: [no-new-privileges:true]
    pids_limit: 256
    mem_limit: 256m
    cpus: "1.0"
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
networks:
  gateway:
    ipam:
      config:
        - subnet: ${{GATEWAY_NETWORK_CIDR:?Set a dedicated edge network CIDR}}
  upstreams:
    external: true
    name: ${{UPSTREAM_NETWORK:?Set the existing backend Docker network}}
volumes:
  caddy_data:
  caddy_config:
''')
    (output / "Caddyfile").write_text('''{
    email {$ACME_EMAIL}
    admin off
}
{$GATEWAY_DOMAIN} {
    @internal path /_gateway /_gateway/*
    respond @internal 404
    reverse_proxy gateway:PORT {
        header_up X-Real-IP {remote_host}
    }
}
'''.replace("PORT", str(config.port)))
    (output / ".env.example").write_text('''GATEWAY_IMAGE=ai-pingora-gateway:release-001
# Set a verified Caddy 2 image digest before deploying.
CADDY_IMAGE=
GATEWAY_DOMAIN=api.example.com
ACME_EMAIL=ops@example.com
UPSTREAM_NETWORK=application-backends
GATEWAY_NETWORK_CIDR=172.30.50.0/24
''')
    return output
