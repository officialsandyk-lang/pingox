from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


@dataclass
class NginxEdgeDecision:
    ok: bool
    decision: str
    root_cause: str | None = None
    message: str = ""
    expected_active_color: str | None = None
    nginx_active_color: str | None = None
    expected_paths: list[str] | None = None
    candidate_paths: list[str] | None = None
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def extract_nginx_upstream_color(nginx_text: str) -> str | None:
    matches = re.findall(r"proxy_pass\s+http://gateway-(blue|green):8090", nginx_text)
    if not matches:
        return None

    unique = sorted(set(matches))
    if len(unique) != 1:
        return "mixed"

    return unique[0]


def validate_edge_config_points_to_color(
    nginx_conf_path: str | Path,
    expected_color: str,
) -> NginxEdgeDecision:
    path = Path(nginx_conf_path)

    if not path.exists():
        return NginxEdgeDecision(
            ok=False,
            decision="block",
            root_cause="nginx_config_missing",
            message=f"NGINX config file missing: {path}",
            expected_active_color=expected_color,
        )

    text = path.read_text(encoding="utf-8")
    actual_color = extract_nginx_upstream_color(text)

    if actual_color != expected_color:
        return NginxEdgeDecision(
            ok=False,
            decision="block",
            root_cause="nginx_edge_wrong_active_color",
            message=f"NGINX config points to {actual_color}, expected {expected_color}",
            expected_active_color=expected_color,
            nginx_active_color=actual_color,
        )

    return NginxEdgeDecision(
        ok=True,
        decision="allow",
        message="NGINX host config points to expected active color",
        expected_active_color=expected_color,
        nginx_active_color=actual_color,
    )


def validate_running_nginx_points_to_color(expected_color: str) -> NginxEdgeDecision:
    result = _run(
        ["docker", "exec", "ai-pingora-gateway-edge", "nginx", "-T"],
        timeout=30,
    )

    output = (result.stdout or "") + "\n" + (result.stderr or "")

    if result.returncode != 0:
        return NginxEdgeDecision(
            ok=False,
            decision="block",
            root_cause="nginx_runtime_config_unreadable",
            message="Could not read running NGINX config with nginx -T",
            expected_active_color=expected_color,
            details={
                "stdout": result.stdout[-1000:],
                "stderr": result.stderr[-1000:],
            },
        )

    actual_color = extract_nginx_upstream_color(output)

    if actual_color != expected_color:
        return NginxEdgeDecision(
            ok=False,
            decision="block",
            root_cause="nginx_runtime_wrong_active_color",
            message=f"Running NGINX points to {actual_color}, expected {expected_color}",
            expected_active_color=expected_color,
            nginx_active_color=actual_color,
        )

    return NginxEdgeDecision(
        ok=True,
        decision="allow",
        message="Running NGINX config points to expected active color",
        expected_active_color=expected_color,
        nginx_active_color=actual_color,
    )


def validate_candidate_container_config(
    candidate_color: str,
    expected_config: dict[str, Any],
) -> NginxEdgeDecision:
    expected_paths = sorted(
        str(r.get("path"))
        for r in expected_config.get("routes", [])
        if r.get("path")
    )

    result = _run(
        [
            "docker",
            "exec",
            f"ai-pingora-gateway-{candidate_color}",
            "cat",
            "/app/config.json",
        ],
        timeout=30,
    )

    if result.returncode != 0:
        return NginxEdgeDecision(
            ok=False,
            decision="block",
            root_cause="candidate_config_unreadable",
            message=f"Cannot read candidate {candidate_color} /app/config.json",
            expected_active_color=candidate_color,
            expected_paths=expected_paths,
            details={
                "stdout": result.stdout[-1000:],
                "stderr": result.stderr[-1000:],
            },
        )

    try:
        candidate_config = json.loads(result.stdout or "{}")
    except Exception as exc:
        return NginxEdgeDecision(
            ok=False,
            decision="block",
            root_cause="candidate_config_invalid_json",
            message=f"Candidate config invalid JSON: {exc}",
            expected_active_color=candidate_color,
            expected_paths=expected_paths,
        )

    candidate_paths = sorted(
        str(r.get("path"))
        for r in candidate_config.get("routes", [])
        if r.get("path")
    )

    if candidate_paths != expected_paths:
        return NginxEdgeDecision(
            ok=False,
            decision="block",
            root_cause="candidate_config_drift",
            message="Candidate container config does not match expected generated config",
            expected_active_color=candidate_color,
            expected_paths=expected_paths,
            candidate_paths=candidate_paths,
        )

    return NginxEdgeDecision(
        ok=True,
        decision="allow",
        message="Candidate container config matches expected generated config",
        expected_active_color=candidate_color,
        expected_paths=expected_paths,
        candidate_paths=candidate_paths,
    )


def validate_candidate_route_live(
    candidate_color: str,
    path: str,
    expected_backend: str | None = None,
) -> NginxEdgeDecision:
    route = "/" + str(path or "/").strip("/")
    if route == "//":
        route = "/"

    url = f"http://gateway-{candidate_color}:8090{route}"
    if not url.endswith("/"):
        url += "/"

    result = _run(
        [
            "docker",
            "exec",
            "ai-pingora-gateway-edge",
            "sh",
            "-lc",
            f"wget -S -qO- {url} 2>&1",
        ],
        timeout=30,
    )

    output = (result.stdout or "") + "\n" + (result.stderr or "")

    if "HTTP/1.0 200 OK" not in output and "HTTP/1.1 200 OK" not in output:
        return NginxEdgeDecision(
            ok=False,
            decision="block",
            root_cause="candidate_route_not_live",
            message=f"Candidate route {route} is not live on gateway-{candidate_color}",
            expected_active_color=candidate_color,
            details={"output": output[-1500:]},
        )

    if expected_backend and expected_backend not in output:
        return NginxEdgeDecision(
            ok=False,
            decision="block",
            root_cause="candidate_route_backend_mismatch",
            message=f"Candidate route {route} did not show expected backend {expected_backend}",
            expected_active_color=candidate_color,
            details={"output": output[-1500:]},
        )

    return NginxEdgeDecision(
        ok=True,
        decision="allow",
        message=f"Candidate route {route} is live",
        expected_active_color=candidate_color,
        details={"output": output[-1000:]},
    )


def raise_if_blocked(decision: NginxEdgeDecision) -> None:
    if not decision.ok:
        raise RuntimeError(
            "NGINX edge decision blocked deployment.\n"
            f"Root cause: {decision.root_cause}\n"
            f"Message: {decision.message}\n"
            f"Details: {decision.to_dict()}"
        )
