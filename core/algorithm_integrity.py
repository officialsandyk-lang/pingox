from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
UTC = timezone.utc
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE_PATH = PROJECT_ROOT / "data" / "algorithm_integrity_baseline.json"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _norm_path(path: Any) -> str:
    text = str(path or "/").strip()

    if not text.startswith("/"):
        text = "/" + text

    if len(text) > 1:
        text = text.rstrip("/")

    return text or "/"


def _route_type(route: dict[str, Any]) -> str:
    return str(route.get("type") or route.get("route_type") or "proxy").strip().lower()


def _norm_balancing(route: dict[str, Any]) -> str:
    route_type = _route_type(route)

    if route_type == "static":
        return "static"

    balancing = route.get("balancing")

    if balancing:
        return str(balancing).strip().lower().replace("-", "_").replace(" ", "_")

    upstreams = _norm_upstreams(route)

    if len(upstreams) <= 1:
        return "single"

    return "round_robin"


def _norm_address(value: Any) -> str:
    text = str(value or "").strip()
    text = text.replace("http://", "").replace("https://", "").rstrip("/")
    return text


def _norm_upstreams(route: dict[str, Any]) -> list[dict[str, Any]]:
    route_type = _route_type(route)

    if route_type == "static":
        return []

    value = route.get("upstreams")
    output: list[dict[str, Any]] = []

    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                address = _norm_address(
                    item.get("address")
                    or item.get("upstream")
                    or item.get("backend")
                )
                weight = int(item.get("weight") or 1)
            else:
                address = _norm_address(item)
                weight = 1

            if address:
                output.append({"address": address, "weight": weight})

        return output

    address = _norm_address(route.get("upstream") or route.get("backend"))

    if address:
        output.append({"address": address, "weight": 1})

    return output


def route_algorithm_signature(route: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": _norm_path(route.get("path")),
        "type": _route_type(route),
        "balancing": _norm_balancing(route),
        "upstreams": _norm_upstreams(route),
    }


def build_algorithm_integrity_baseline(
    config: dict[str, Any],
    *,
    source: str = "manual",
) -> dict[str, Any]:
    routes = config.get("routes") or []

    route_signatures: dict[str, dict[str, Any]] = {}

    for route in routes:
        if not isinstance(route, dict):
            continue

        sig = route_algorithm_signature(route)
        route_signatures[sig["path"]] = sig

    payload = {
        "created_at_utc": _utc_now(),
        "source": source,
        "runtime": config.get("runtime") or config.get("runtime_mode"),
        "runtime_mode": config.get("runtime_mode") or config.get("runtime"),
        "strategy": config.get("strategy") or config.get("deployment_strategy"),
        "deployment_strategy": config.get("deployment_strategy") or config.get("strategy"),
        "routes": route_signatures,
    }

    payload["integrity_hash"] = hashlib.sha256(
        json.dumps(payload["routes"], sort_keys=True).encode("utf-8")
    ).hexdigest()

    return payload


def save_algorithm_integrity_baseline(
    config: dict[str, Any],
    *,
    baseline_path: str | Path = DEFAULT_BASELINE_PATH,
    source: str = "manual",
) -> dict[str, Any]:
    baseline_path = Path(baseline_path)
    baseline_path.parent.mkdir(parents=True, exist_ok=True)

    baseline = build_algorithm_integrity_baseline(config, source=source)
    baseline_path.write_text(json.dumps(baseline, indent=2), encoding="utf-8")

    return baseline


def load_algorithm_integrity_baseline(
    baseline_path: str | Path = DEFAULT_BASELINE_PATH,
) -> dict[str, Any] | None:
    path = Path(baseline_path)

    if not path.exists():
        return None

    return json.loads(path.read_text(encoding="utf-8"))


def validate_algorithm_integrity(
    config: dict[str, Any],
    *,
    baseline: dict[str, Any] | None = None,
    baseline_path: str | Path = DEFAULT_BASELINE_PATH,
    require_baseline: bool = True,
    allow_extra_routes: bool = False,
    allowed_changed_paths: set[str] | None = None,
) -> dict[str, Any]:
    allowed_changed_paths = {_norm_path(path) for path in (allowed_changed_paths or set())}

    if baseline is None:
        baseline = load_algorithm_integrity_baseline(baseline_path)

    if baseline is None:
        return {
            "success": not require_baseline,
            "baseline_present": False,
            "failures": ["Algorithm integrity baseline missing"] if require_baseline else [],
            "baseline_path": str(baseline_path),
        }

    current = build_algorithm_integrity_baseline(config, source="current_check")
    expected_routes = baseline.get("routes") or {}
    actual_routes = current.get("routes") or {}

    failures: list[str] = []

    for path, expected in expected_routes.items():
        path = _norm_path(path)

        if path in allowed_changed_paths:
            continue

        actual = actual_routes.get(path)

        if actual is None:
            failures.append(f"{path}: route missing from current config")
            continue

        for key in ["type", "balancing", "upstreams"]:
            if actual.get(key) != expected.get(key):
                failures.append(
                    f"{path}: {key} drift detected. "
                    f"expected={expected.get(key)} actual={actual.get(key)}"
                )

    if not allow_extra_routes:
        for path in actual_routes:
            path = _norm_path(path)

            if path not in expected_routes and path not in allowed_changed_paths:
                failures.append(f"{path}: unexpected route not present in algorithm baseline")

    return {
        "success": not failures,
        "baseline_present": True,
        "baseline_path": str(baseline_path),
        "expected_hash": baseline.get("integrity_hash"),
        "actual_hash": current.get("integrity_hash"),
        "failures": failures,
        "expected_routes": expected_routes,
        "actual_routes": actual_routes,
    }
