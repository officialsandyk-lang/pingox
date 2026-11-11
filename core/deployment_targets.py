"""Portable deployment contracts. Cloud accounts are never mutated by generation."""
from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, field_validator, model_validator
from core.production import StrictModel

TARGETS = ("compose", "container", "kubernetes", "systemd")


class DeploymentOptions(StrictModel):
    target: Literal["compose", "container", "kubernetes", "systemd"] = "compose"
    image: str | None = None
    namespace: str = "ai-gateway"
    replicas: int = Field(default=2, ge=1, le=100)
    trusted_proxy_cidrs: list[str] = Field(default_factory=list)

    @field_validator("namespace")
    @classmethod
    def namespace_valid(cls, value):
        if len(value) > 63 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", value):
            raise ValueError("namespace must be a Kubernetes DNS label")
        return value

    @field_validator("image")
    @classmethod
    def image_valid(cls, value):
        if value is not None:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*", value):
                raise ValueError("invalid image reference")
            leaf = value.rsplit("/", 1)[-1]
            if ":" not in leaf or leaf.endswith(":latest"):
                raise ValueError("use a versioned image tag or digest, not latest")
        return value

    @field_validator("trusted_proxy_cidrs")
    @classmethod
    def cidrs_valid(cls, values):
        for value in values:
            network = ipaddress.ip_network(value, strict=True)
            if network.prefixlen == 0:
                raise ValueError("trust only dedicated proxy networks, never all addresses")
        return values

    @model_validator(mode="after")
    def target_requirements(self):
        if self.target == "kubernetes" and self.image is None:
            raise ValueError("Kubernetes requires a built image tag or digest")
        if self.target not in {"kubernetes", "systemd"} and self.trusted_proxy_cidrs:
            raise ValueError("set trusted proxy networks in the target runtime environment for this target")
        return self


def write_target(output: Path, port: int, options: DeploymentOptions) -> None:
    (output / "deployment.json").write_text(options.model_dump_json(indent=2) + "\n")
    if options.target in {"compose", "container"}:
        return
    if options.target == "systemd":
        (output / "ai-pingora-gateway.service").write_text(f'''[Unit]
Description=AI Pingora Gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
DynamicUser=yes
WorkingDirectory=/opt/ai-pingora-gateway/current
ExecStart=/opt/ai-pingora-gateway/current/gateway
Environment=GATEWAY_BIND_HOST=127.0.0.1
Environment=TRUSTED_PROXY_CIDRS={','.join(options.trusted_proxy_cidrs)}
Restart=on-failure
RestartSec=3
TimeoutStopSec=45
KillSignal=SIGTERM
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
MemoryMax=512M
CPUQuota=200%
TasksMax=256
LimitNOFILE=65536
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
''')
        return
    labels = {"app.kubernetes.io/name": "ai-pingora-gateway"}
    meta = {"name": "ai-pingora-gateway", "namespace": options.namespace}
    probe = {"httpGet": {"path": "/_gateway/health", "port": "http"}, "timeoutSeconds": 2}
    documents = [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": options.namespace}},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta,
         "spec": {"replicas": options.replicas, "revisionHistoryLimit": 3,
                  "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1}},
                  "selector": {"matchLabels": labels},
                  "template": {"metadata": {"labels": labels}, "spec": {
                      "automountServiceAccountToken": False,
                      "terminationGracePeriodSeconds": 45,
                      "securityContext": {"runAsNonRoot": True, "runAsUser": 65532, "runAsGroup": 65532,
                                          "seccompProfile": {"type": "RuntimeDefault"}},
                      "containers": [{"name": "gateway", "image": options.image, "imagePullPolicy": "IfNotPresent",
                         "ports": [{"name": "http", "containerPort": port}],
                         "env": [{"name": "TRUSTED_PROXY_CIDRS", "value": ",".join(options.trusted_proxy_cidrs)}],
                         "securityContext": {"readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False,
                                             "capabilities": {"drop": ["ALL"]}},
                         "resources": {"requests": {"cpu": "100m", "memory": "64Mi"},
                                       "limits": {"cpu": "2", "memory": "512Mi"}},
                         "startupProbe": {**probe, "periodSeconds": 2, "failureThreshold": 30},
                         "readinessProbe": {**probe, "periodSeconds": 5, "failureThreshold": 2},
                         "livenessProbe": {**probe, "periodSeconds": 10, "failureThreshold": 3}}]}}}},
        {"apiVersion": "v1", "kind": "Service", "metadata": meta,
         "spec": {"type": "ClusterIP", "selector": labels, "ports": [{"name": "http", "port": port, "targetPort": "http"}]}},
        {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": meta,
         "spec": {"minAvailable": 1, "selector": {"matchLabels": labels}}},
    ]
    (output / "kubernetes.yaml").write_text(yaml.safe_dump_all(documents, sort_keys=False))


def validate_target(config, options: DeploymentOptions) -> None:
    if options.target == "systemd":
        return
    for route in config.routes:
        for upstream in route.upstreams:
            host = upstream.address.rsplit(":", 1)[0]
            loopback = host == "localhost"
            try:
                loopback = loopback or ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
            if loopback:
                raise ValueError("container targets require reachable backend DNS/IP addresses, not loopback")
