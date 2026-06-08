#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from core.environment_deployer import write_nginx_conf
from core.nginx_config_guard import validate_nginx_file


DOCKER_NGINX_CONF = PROJECT_ROOT / "docker-bluegreen" / "nginx.conf"


def detect_active_color() -> str:
    """
    Best-effort active colour detection without depending on deployment_state.load_state().
    """
    candidates = [
        PROJECT_ROOT / "data" / "deployment_state.json",
        PROJECT_ROOT / "data" / "docker_bluegreen_state.json",
        PROJECT_ROOT / "data" / "runtime_state.json",
        PROJECT_ROOT / "data" / "active_deployment.json",
    ]

    for path in candidates:
        if not path.exists():
            continue

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue

        for key in ("active_color", "active_colour", "color", "colour"):
            value = str(data.get(key) or "").strip().lower()
            if value in {"blue", "green"}:
                return value

    env_color = str(os.getenv("PINGORA_ACTIVE_COLOR") or "").strip().lower()
    if env_color in {"blue", "green"}:
        return env_color

    # Safe default: existing tests normally start with blue; transactional update can still rewrite later.
    return "blue"


def main() -> int:
    active_color = detect_active_color()
    gateway_port = int(os.getenv("PINGORA_GATEWAY_PORT", "8090"))

    write_nginx_conf(active_color, gateway_port)
    validate_nginx_file(DOCKER_NGINX_CONF)

    print("✅ Repaired and validated docker-bluegreen/nginx.conf")
    print(f"active_color={active_color}")
    print(f"gateway_port={gateway_port}")
    print(f"file={DOCKER_NGINX_CONF}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
