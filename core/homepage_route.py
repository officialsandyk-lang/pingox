from __future__ import annotations

from copy import deepcopy
from typing import Any


def normalize_path(path: Any) -> str:
    value = str(path or "/").strip()

    if not value:
        return "/"

    if not value.startswith("/"):
        value = "/" + value

    if len(value) > 1:
        value = value.rstrip("/")

    return value


def _static_homepage_route() -> dict[str, Any]:
    return {
        "path": "/",
        "type": "static",
        "balancing": "single",
        "static_dir": "public",
        "index_file": "index.html",
    }


def ensure_route_directory_homepage(config: dict[str, Any]) -> dict[str, Any]:
    """
    Runtime-neutral guarantee:
    every generated gateway exposes "/" as the route-directory homepage.

    Important:
    If "/" already exists but was accidentally generated as a proxy route,
    this function force-converts it to static homepage.
    """

    if not isinstance(config, dict):
        return config

    config = deepcopy(config)

    routes = config.setdefault("routes", [])

    if not isinstance(routes, list):
        routes = []
        config["routes"] = routes

    cleaned_routes: list[dict[str, Any]] = []
    homepage_added = False

    for route in routes:
        if not isinstance(route, dict):
            continue

        path = normalize_path(route.get("path"))

        if path == "/":
            if not homepage_added:
                cleaned_routes.append(_static_homepage_route())
                homepage_added = True
            # Drop duplicate/bad "/" routes, especially proxy-to-8080.
            continue

        route["path"] = path
        cleaned_routes.append(route)

    if not homepage_added:
        cleaned_routes.insert(0, _static_homepage_route())

    config["routes"] = cleaned_routes

    return config
