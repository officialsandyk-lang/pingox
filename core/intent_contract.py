from __future__ import annotations

import copy
import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "reliability_reports.sqlite3"


SUPPORTED_ALGORITHMS = {
    "random",
    "round_robin",
    "weighted_round_robin",
    "ip_hash",
    "least_connections",
}


@dataclass
class ExpectedRoute:
    path: str
    route_type: str = "proxy"
    balancing: str | None = None
    upstreams: list[str] = field(default_factory=list)
    weights: dict[str, int] = field(default_factory=dict)
    root: str = "public"
    index: str = "index.html"
    source_text: str = ""


@dataclass
class IntentContract:
    source_prompt: str
    routes: dict[str, ExpectedRoute] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_prompt": self.source_prompt,
            "routes": {
                path: asdict(route)
                for path, route in self.routes.items()
            },
        }


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_path(path: Any) -> str:
    text = str(path or "/").strip()

    if not text.startswith("/"):
        text = "/" + text

    text = re.sub(r"[^a-zA-Z0-9/_\-.]", "", text)

    if len(text) > 1:
        text = text.rstrip("/")

    return text or "/"


def _normalize_address(value: Any) -> str:
    text = str(value or "").strip()
    text = text.replace("localhost", "127.0.0.1")
    text = text.replace("http://", "").replace("https://", "").rstrip("/")

    if "/" in text:
        text = text.split("/", 1)[0]

    return text


def _port(value: Any) -> str | None:
    text = _normalize_address(value)

    if ":" not in text:
        return None

    port = text.rsplit(":", 1)[-1]

    if port.isdigit():
        return port

    return None


def _address_port_set(addresses: list[str]) -> set[str]:
    ports: set[str] = set()

    for address in addresses:
        port = _port(address)

        if port:
            ports.add(port)

    return ports


def _is_static_route(route: dict[str, Any]) -> bool:
    return str(route.get("type") or "").lower() in {
        "static",
        "web",
        "webserver",
        "web_server",
        "file_server",
        "files",
    }


def _route_upstreams(route: dict[str, Any]) -> list[str]:
    upstreams = route.get("upstreams")
    output: list[str] = []

    if isinstance(upstreams, list):
        for item in upstreams:
            if isinstance(item, dict):
                address = (
                    item.get("address")
                    or item.get("upstream")
                    or item.get("backend")
                )
            else:
                address = item

            if address:
                output.append(_normalize_address(address))

    else:
        address = route.get("upstream") or route.get("backend")

        if address:
            output.append(_normalize_address(address))

    return output


def _route_weights(route: dict[str, Any]) -> dict[str, int]:
    upstreams = route.get("upstreams")
    output: dict[str, int] = {}

    if not isinstance(upstreams, list):
        return output

    for item in upstreams:
        if not isinstance(item, dict):
            continue

        address = item.get("address") or item.get("upstream") or item.get("backend")
        port = _port(address)

        if not port:
            continue

        try:
            weight = int(item.get("weight", 1))
        except Exception:
            weight = 1

        output[port] = max(weight, 1)

    return output


def _split_prompt(prompt: str) -> list[str]:
    text = str(prompt or "").strip()

    if not text:
        return []

    parts = re.split(
        r";|\n|(?<=[.!?])\s+(?=(Create|Add|Route|Load|Serve|Set|Allow|Block|Enable|Please|I)\b)",
        text,
        flags=re.IGNORECASE,
    )

    clauses: list[str] = []

    for part in parts:
        if not part:
            continue

        cleaned = str(part).strip()

        if not cleaned:
            continue

        # Ignore regex capture words created by split().
        if cleaned.lower() in {
            "create",
            "add",
            "route",
            "load",
            "serve",
            "set",
            "allow",
            "block",
            "enable",
            "please",
            "i",
        }:
            continue

        clauses.append(cleaned)

    return clauses


def _extract_paths(text: str) -> list[str]:
    candidates = re.findall(r"(?<![\w.])/[A-Za-z0-9_\-./]*", text)
    output: list[str] = []

    for item in candidates:
        path = _normalize_path(item)

        if path and path not in output:
            output.append(path)

    return output


def _extract_addresses(text: str) -> list[str]:
    pattern = r"(?:localhost|127\.0\.0\.1|\d{1,3}(?:\.\d{1,3}){3}):\d{2,5}"
    addresses = re.findall(pattern, text, flags=re.IGNORECASE)
    output: list[str] = []

    for address in addresses:
        normalized = _normalize_address(address)

        if normalized not in output:
            output.append(normalized)

    return output


def _extract_weights(text: str) -> dict[str, int]:
    weights: dict[str, int] = {}

    # 127.0.0.1:9201 weight 5
    pattern = r"((?:localhost|127\.0\.0\.1|\d{1,3}(?:\.\d{1,3}){3}):\d{2,5})\s*(?:with\s*)?(?:weight|weighted)\s*(\d+)"

    for address, weight_raw in re.findall(pattern, text, flags=re.IGNORECASE):
        port = _port(address)

        if not port:
            continue

        try:
            weights[port] = max(int(weight_raw), 1)
        except Exception:
            weights[port] = 1

    return weights


def _detect_algorithm(text: str, addresses: list[str], weights: dict[str, int]) -> str | None:
    lower = text.lower()

    if "weighted round robin" in lower or "weighted_round_robin" in lower:
        return "weighted_round_robin"

    if weights:
        return "weighted_round_robin"

    if "least connections" in lower or "least_connections" in lower:
        return "least_connections"

    if "ip hash" in lower or "ip_hash" in lower or "sticky" in lower:
        return "ip_hash"

    if "round robin" in lower or "round_robin" in lower:
        return "round_robin"

    if "random" in lower:
        return "random"

    if len(addresses) > 1 and ("balance" in lower or "balanced" in lower or "across" in lower):
        return "round_robin"

    return None


def _is_security_clause(text: str) -> bool:
    lower = text.lower().strip()

    return lower.startswith(("block ", "allow ", "set rate", "set max", "enable crud", "add production-style local protections"))


def _is_route_clause(text: str) -> bool:
    lower = text.lower()

    return any(
        word in lower
        for word in [
            "create ",
            "add ",
            "route ",
            "load balance",
            "balanced across",
            "to backend",
            "using ",
            "serve ",
        ]
    )


def build_intent_contract(prompt: str) -> IntentContract:
    contract = IntentContract(source_prompt=prompt)

    lower_prompt = prompt.lower()

    # Remember the most recently explicit upstream list so phrases like
    # "same three backends" or "same backends" can be resolved safely.
    last_explicit_upstreams: list[str] = []

    # Always treat explicit homepage/static webserver wording as / static.
    if (
        "route-directory homepage" in lower_prompt
        or "route directory homepage" in lower_prompt
        or "homepage at /" in lower_prompt
        or "serving public at /" in lower_prompt
        or "serve public at /" in lower_prompt
        or "webserver on port" in lower_prompt
    ):
        contract.routes["/"] = ExpectedRoute(
            path="/",
            route_type="static",
            root="public",
            index="index.html",
            source_text="homepage/static webserver request",
        )

    for clause in _split_prompt(prompt):
        if _is_security_clause(clause):
            continue

        paths = _extract_paths(clause)

        if not paths:
            continue

        # Skip blocked-path-only clauses.
        if "block" in clause.lower() and not _is_route_clause(clause):
            continue

        path = paths[0]
        addresses = _extract_addresses(clause)
        weights = _extract_weights(clause)
        lower_clause = clause.lower()

        # Resolve natural newbie wording:
        # "same three backends", "same backends", "same three backend servers".
        if not addresses and last_explicit_upstreams and re.search(
            r"\bsame\b.*\bbackend", lower_clause
        ):
            addresses = list(last_explicit_upstreams)

        algorithm = _detect_algorithm(clause, addresses, weights)

        if path == "/":
            if (
                "homepage" in lower_clause
                or "public" in lower_clause
                or "webserver" in lower_clause
                or "serve" in lower_clause
            ):
                contract.routes["/"] = ExpectedRoute(
                    path="/",
                    route_type="static",
                    root="public",
                    index="index.html",
                    source_text=clause,
                )
            continue

        if not addresses and not algorithm:
            continue

        expected = ExpectedRoute(
            path=path,
            route_type="proxy",
            balancing=algorithm,
            upstreams=addresses,
            weights=weights,
            source_text=clause,
        )

        if len(addresses) == 1 and not algorithm:
            expected.balancing = None

        contract.routes[path] = expected

        # Only update memory when the clause explicitly named addresses.
        # Do not let "same backends" overwrite the remembered list.
        explicit_addresses = _extract_addresses(clause)

        if explicit_addresses:
            last_explicit_upstreams = explicit_addresses

    return contract


def _route_map(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}

    routes = config.get("routes") or []

    if not isinstance(routes, list):
        return output

    for route in routes:
        if not isinstance(route, dict):
            continue

        output[_normalize_path(route.get("path"))] = route

    return output


def validate_config_against_contract(
    config: dict[str, Any],
    contract: IntentContract | dict[str, Any],
) -> dict[str, Any]:
    if isinstance(contract, dict):
        contract = contract_from_dict(contract)

    route_map = _route_map(config)
    failures: list[dict[str, Any]] = []

    for path, expected in contract.routes.items():
        actual = route_map.get(path)

        if not actual:
            failures.append(
                {
                    "path": path,
                    "code": "missing_route",
                    "message": f"Expected route {path} is missing.",
                    "expected": asdict(expected),
                    "actual": None,
                }
            )
            continue

        if expected.route_type == "static":
            if not _is_static_route(actual):
                failures.append(
                    {
                        "path": path,
                        "code": "route_type_mismatch",
                        "message": f"{path} should be static homepage, but config generated proxy route.",
                        "expected": "static",
                        "actual": actual.get("type") or "proxy",
                    }
                )

            if actual.get("upstream") or actual.get("backend") or actual.get("upstreams"):
                failures.append(
                    {
                        "path": path,
                        "code": "static_route_has_upstreams",
                        "message": f"{path} static route should not have backend/upstreams.",
                        "actual": copy.deepcopy(actual),
                    }
                )

            continue

        actual_ports = _address_port_set(_route_upstreams(actual))
        expected_ports = _address_port_set(expected.upstreams)

        if expected_ports and actual_ports != expected_ports:
            failures.append(
                {
                    "path": path,
                    "code": "upstream_mismatch",
                    "message": f"{path} upstream ports do not match user intent.",
                    "expected_ports": sorted(expected_ports),
                    "actual_ports": sorted(actual_ports),
                }
            )

        expected_algorithm = expected.balancing
        actual_algorithm = actual.get("balancing")

        if expected_algorithm and actual_algorithm != expected_algorithm:
            failures.append(
                {
                    "path": path,
                    "code": "algorithm_mismatch",
                    "message": f"{path} should use {expected_algorithm}, but config has {actual_algorithm}.",
                    "expected": expected_algorithm,
                    "actual": actual_algorithm,
                }
            )

        if not expected_algorithm and len(expected_ports) <= 1:
            # Single upstream routes may omit balancing or use runtime default.
            pass

        if expected.weights:
            actual_weights = _route_weights(actual)

            if actual_weights != expected.weights:
                failures.append(
                    {
                        "path": path,
                        "code": "weight_mismatch",
                        "message": f"{path} weights do not match user intent.",
                        "expected": expected.weights,
                        "actual": actual_weights,
                    }
                )

    return {
        "success": not failures,
        "failure_count": len(failures),
        "failures": failures,
        "contract": contract.to_dict(),
    }


def contract_from_dict(data: dict[str, Any]) -> IntentContract:
    routes = {}

    for path, raw in (data.get("routes") or {}).items():
        routes[path] = ExpectedRoute(**raw)

    return IntentContract(
        source_prompt=data.get("source_prompt") or "",
        routes=routes,
    )


def _route_from_expected(expected: ExpectedRoute) -> dict[str, Any]:
    if expected.route_type == "static":
        return {
            "path": expected.path,
            "type": "static",
            "root": expected.root or "public",
            "index": expected.index or "index.html",
        }

    route: dict[str, Any] = {
        "path": expected.path,
    }

    if expected.upstreams:
        route["upstream"] = expected.upstreams[0]
        route["backend"] = expected.upstreams[0]

        if expected.weights:
            weighted_upstreams = []

            for address in expected.upstreams:
                port = _port(address)
                weighted_upstreams.append(
                    {
                        "address": address,
                        "weight": int(expected.weights.get(port or "", 1)),
                    }
                )

            route["upstreams"] = weighted_upstreams
        elif len(expected.upstreams) > 1:
            route["upstreams"] = expected.upstreams
        else:
            route["upstream"] = expected.upstreams[0]
            route["backend"] = expected.upstreams[0]

    if expected.balancing:
        route["balancing"] = expected.balancing

    return route


def repair_config_from_contract(
    config: dict[str, Any],
    contract: IntentContract | dict[str, Any],
) -> dict[str, Any]:
    if isinstance(contract, dict):
        contract = contract_from_dict(contract)

    fixed = copy.deepcopy(config)
    existing_routes = fixed.get("routes") or []

    if not isinstance(existing_routes, list):
        existing_routes = []

    expected_paths = set(contract.routes.keys())
    repaired_routes: list[dict[str, Any]] = []

    # Put / static first when expected.
    if "/" in contract.routes:
        repaired_routes.append(_route_from_expected(contract.routes["/"]))

    # Then all non-root expected routes in prompt order.
    for path, expected in contract.routes.items():
        if path == "/":
            continue

        repaired_routes.append(_route_from_expected(expected))

    # Preserve routes not mentioned in the prompt contract.
    for route in existing_routes:
        if not isinstance(route, dict):
            continue

        path = _normalize_path(route.get("path"))

        if path in expected_paths:
            continue

        if path == "/" and "/" in expected_paths:
            continue

        repaired_routes.append(route)

    fixed["routes"] = repaired_routes

    return fixed


def _ensure_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS intent_contract_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                source TEXT,
                success INTEGER NOT NULL,
                repaired INTEGER NOT NULL,
                prompt TEXT,
                contract_json TEXT,
                validation_json TEXT,
                repair_summary TEXT
            )
            """
        )
        conn.commit()


def store_intent_contract_event(
    *,
    prompt: str,
    contract: IntentContract,
    validation: dict[str, Any],
    repaired: bool,
    source: str = "main",
    db_path: str | Path = DEFAULT_DB_PATH,
) -> None:
    db_path = Path(db_path)
    _ensure_db(db_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO intent_contract_events (
                created_at,
                source,
                success,
                repaired,
                prompt,
                contract_json,
                validation_json,
                repair_summary
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _utc_now(),
                source,
                1 if validation.get("success") else 0,
                1 if repaired else 0,
                prompt,
                json.dumps(contract.to_dict(), indent=2),
                json.dumps(validation, indent=2, default=str),
                "; ".join(
                    f"{item.get('path')}:{item.get('code')}"
                    for item in validation.get("failures") or []
                ),
            ),
        )
        conn.commit()


def enforce_intent_contract(
    *,
    prompt: str,
    config: dict[str, Any],
    source: str = "main",
    auto_repair: bool = True,
    store: bool = True,
) -> dict[str, Any]:
    contract = build_intent_contract(prompt)

    # No expected routes extracted; do not block.
    if not contract.routes:
        return {
            "success": True,
            "changed": False,
            "repaired": False,
            "config": config,
            "contract": contract.to_dict(),
            "validation": {
                "success": True,
                "failure_count": 0,
                "failures": [],
                "reason": "no_intent_routes_extracted",
            },
        }

    validation = validate_config_against_contract(config, contract)

    if validation["success"]:
        if store:
            store_intent_contract_event(
                prompt=prompt,
                contract=contract,
                validation=validation,
                repaired=False,
                source=source,
            )

        return {
            "success": True,
            "changed": False,
            "repaired": False,
            "config": config,
            "contract": contract.to_dict(),
            "validation": validation,
        }

    if not auto_repair:
        if store:
            store_intent_contract_event(
                prompt=prompt,
                contract=contract,
                validation=validation,
                repaired=False,
                source=source,
            )

        return {
            "success": False,
            "changed": False,
            "repaired": False,
            "config": config,
            "contract": contract.to_dict(),
            "validation": validation,
        }

    repaired_config = repair_config_from_contract(config, contract)
    second_validation = validate_config_against_contract(repaired_config, contract)

    if store:
        store_intent_contract_event(
            prompt=prompt,
            contract=contract,
            validation=validation,
            repaired=True,
            source=source,
        )

    return {
        "success": second_validation["success"],
        "changed": repaired_config != config,
        "repaired": True,
        "config": repaired_config,
        "contract": contract.to_dict(),
        "validation": second_validation,
        "initial_validation": validation,
    }


def format_intent_contract_report(result: dict[str, Any]) -> str:
    validation = result.get("validation") or {}
    initial = result.get("initial_validation") or validation

    lines = [
        "📋 Prompt intent contract check",
        "",
        f"Success: {result.get('success')}",
        f"Changed config: {result.get('changed')}",
        f"Repaired: {result.get('repaired')}",
        f"Expected routes: {len((result.get('contract') or {}).get('routes') or {})}",
    ]

    failures = initial.get("failures") or []

    if failures:
        lines.append("")
        lines.append("Detected mismatches:")

        for item in failures:
            lines.append(
                f"- {item.get('path')} | {item.get('code')} | {item.get('message')}"
            )

    if result.get("success"):
        lines.append("")
        lines.append("Intent contract passed ✅")

    return "\n".join(lines)
