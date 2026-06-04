from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class BareMetalState:
    active_color: str
    active_port: int
    previous_color: str | None = None
    previous_port: int | None = None


def opposite_color(color: str) -> str:
    return "green" if color == "blue" else "blue"


def color_port(color: str) -> int:
    """
    Temporary bare-metal proof-mode port mapping.

    Current generated Rust runtime ignores --port and always binds to 8090.
    Until dynamic --port support lands, both blue and green must use 8090.
    """
    return 8090


def default_state() -> BareMetalState:
    return BareMetalState(active_color="blue", active_port=color_port("blue"))


def parse_state(text: str | None) -> BareMetalState:
    if not text:
        return default_state()

    try:
        data = json.loads(text)
    except Exception:
        return default_state()

    active_color = str(data.get("active_color") or "blue")
    if active_color not in {"blue", "green"}:
        active_color = "blue"

    return BareMetalState(
        active_color=active_color,
        active_port=int(data.get("active_port") or color_port(active_color)),
        previous_color=data.get("previous_color"),
        previous_port=data.get("previous_port"),
    )


def serialize_state(state: BareMetalState) -> str:
    return json.dumps(
        {
            "active_color": state.active_color,
            "active_port": state.active_port,
            "previous_color": state.previous_color,
            "previous_port": state.previous_port,
        },
        indent=2,
        sort_keys=True,
    )
