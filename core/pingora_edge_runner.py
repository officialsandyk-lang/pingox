from __future__ import annotations

from pathlib import Path
from typing import Any

from core.pingora_edge_writer import write_pingora_edge_config


def prepare_pingora_edge(config: dict[str, Any], output_path: str | Path) -> dict[str, Any]:
    path = write_pingora_edge_config(config, output_path)
    return {
        "success": True,
        "edge_type": "pingora",
        "config_path": str(path),
        "wired": False,
        "message": "Pingora edge config written. Runtime wiring is not enabled yet.",
    }
