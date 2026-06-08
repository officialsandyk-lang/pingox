from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.ports import PUBLIC_HTTP_PORT, PUBLIC_HTTPS_PORT


def build_pingora_edge_config(config: dict[str, Any]) -> dict[str, Any]:
    edge = dict(config.get("edge") or {})
    edge["type"] = "pingora"
    edge["enabled"] = True

    return {
        "edge": edge,
        "listen": {
            "http": int(config.get("listen_port") or PUBLIC_HTTP_PORT),
            "https": int(config.get("https_port") or PUBLIC_HTTPS_PORT),
        },
        "security": config.get("security") or {},
        "routes": config.get("routes") or [],
        "bluegreen": config.get("bluegreen") or {},
    }


def write_pingora_edge_config(config: dict[str, Any], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_pingora_edge_config(config)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path
