from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "generated-pingora-proxy" / "config.json"
AUDIT_PATH = PROJECT_ROOT / "data" / "audit" / "mutation_decisions.jsonl"


VALID_ALGORITHMS = {
    "single",
    "random",
    "round_robin",
    "weighted_round_robin",
    "ip_hash",
    "least_connections",
}

BLOCKED_ROUTE_PREFIXES = {
    "/.env",
    "/.git",
    "/private",
    "/internal",
    "/debug",
    "/admin.php",
    "/wp-admin",
    "/wp-login.php",
    "/phpmyadmin",
}

DEFAULT_PROTECTED_ROUTES = {
    "/",
    "/api",
}

# Stress-test exception is intentionally narrow.
# It can auto-confirm mutation only for temporary/non-core test routes.
STRESS_TEST_ROUTE_PREFIXES = (
    "/test",
    "/tmp",
    "/stress",
    "/coupons",
    "/billing",
    "/audit",
)


@dataclass
class MutationDecision:
    allowed: bool
    action: str
    route: str | None
    risk: str
    reason: str
    requires_confirmation: bool
    confirmed: bool
    checks: list[dict[str, Any]]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def normalize_path(value: Any) -> str | None:
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    if not text.startswith("/"):
        text = "/" + text

    text = re.sub(r"[^a-zA-Z0-9/_\-.]", "", text)

    if len(text) > 1:
        text = text.rstrip("/")

    return text or "/"


def load_current_config(path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def route_paths(config: dict[str, Any]) -> set[str]:
    routes = config.get("routes", [])
    if not isinstance(routes, list):
        return set()

    out: set[str] = set()
    for route in routes:
        if isinstance(route, dict):
            path = normalize_path(route.get("path"))
            if path:
                out.add(path)

    return out


def protected_routes(config: dict[str, Any]) -> set[str]:
    security = config.get("security", {})
    mutation_policy = {}

    if isinstance(security, dict):
        mutation_policy = security.get("mutation_policy") or {}

    configured = mutation_policy.get("protected_routes") if isinstance(mutation_policy, dict) else None

    result = set(DEFAULT_PROTECTED_ROUTES)

    if isinstance(configured, list):
        for item in configured:
            path = normalize_path(item)
            if path:
                result.add(path)

    return result


def detect_action(prompt: str) -> str:
    text = prompt.lower().strip()

    if re.search(r"\b(add|create|attach|new route)\b", text):
        return "add"

    if re.search(r"\b(remove|delete|drop)\b", text):
        return "remove"

    if re.search(r"\b(edit|change|set|update|switch)\b", text):
        return "edit"

    if re.search(r"\brollback\b", text):
        return "rollback"

    return "unknown"


def detect_route(prompt: str) -> str | None:
    text = str(prompt or "").strip()

    # Explicit root route mutation, e.g.:
    #   remove /
    #   delete /
    #   add / to backend ...
    # The older regex only detected paths like /api and missed bare /.
    if re.search(r"(^|\s)/(\s|$)", text):
        return "/"

    match = re.search(r"(/[a-zA-Z0-9][a-zA-Z0-9/_\-.]*)", text)
    if not match:
        return None

    return normalize_path(match.group(1))


def detect_algorithm(prompt: str) -> str | None:
    text = prompt.lower()

    aliases = {
        "round robin": "round_robin",
        "round_robin": "round_robin",
        "weighted round robin": "weighted_round_robin",
        "weighted_round_robin": "weighted_round_robin",
        "ip hash": "ip_hash",
        "ip_hash": "ip_hash",
        "least connections": "least_connections",
        "least_connections": "least_connections",
        "random": "random",
        "single": "single",
    }

    for key, value in aliases.items():
        if key in text:
            return value

    return None


def is_blocked_route(path: str | None) -> bool:
    if not path:
        return False

    lowered = path.lower()

    for blocked in BLOCKED_ROUTE_PREFIXES:
        if lowered == blocked or lowered.startswith(blocked + "/"):
            return True

    return False


def audit_decision(decision: MutationDecision, prompt: str) -> None:
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)

    entry = {
        "created_at": _now(),
        "prompt": prompt,
        "decision": asdict(decision),
    }

    with AUDIT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")



def stress_test_mode_enabled() -> bool:
    return os.getenv("PINGORA_AI_STRESS_TEST", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def is_stress_test_route(path: str | None) -> bool:
    if not path:
        return False

    lowered = path.lower()
    return any(
        lowered == prefix or lowered.startswith(prefix)
        for prefix in STRESS_TEST_ROUTE_PREFIXES
    )


def can_auto_confirm_for_stress_test(
    *,
    action: str,
    route: str | None,
    protected: set[str],
) -> bool:
    if not stress_test_mode_enabled():
        return False

    if action not in {"add", "edit", "remove", "delete"}:
        return False

    if not route:
        return False

    # Never auto-confirm protected/core routes.
    if route in protected:
        return False

    # Never auto-confirm root.
    if route == "/":
        return False

    # Never auto-confirm blocked security paths.
    if is_blocked_route(route):
        return False

    return is_stress_test_route(route)


def evaluate_mutation_decision(
    *,
    prompt: str,
    config: dict[str, Any],
    confirmed: bool = False,
) -> MutationDecision:
    action = detect_action(prompt)
    route = detect_route(prompt)
    algorithm = detect_algorithm(prompt)
    existing_routes = route_paths(config)
    protected = protected_routes(config)

    checks: list[dict[str, Any]] = []

    checks.append({
        "name": "mutation action detected",
        "ok": action != "unknown",
        "action": action,
    })

    checks.append({
        "name": "route detected when required",
        "ok": action in {"rollback", "unknown"} or route is not None,
        "route": route,
    })

    if route:
        checks.append({
            "name": "route is not blocked security path",
            "ok": not is_blocked_route(route),
            "route": route,
        })

    if action == "edit" and algorithm is not None:
        checks.append({
            "name": "algorithm is valid",
            "ok": algorithm in VALID_ALGORITHMS,
            "algorithm": algorithm,
        })

    if action == "add" and route:
        checks.append({
            "name": "route does not already exist",
            "ok": route not in existing_routes,
            "route": route,
        })

    if action in {"remove", "delete"} and route:
        checks.append({
            "name": "route exists before remove",
            "ok": route in existing_routes,
            "route": route,
        })

    if action in {"remove", "delete"} and route:
        checks.append({
            "name": "root route cannot be deleted",
            "ok": route != "/",
            "route": route,
        })

    dangerous = False
    requires_confirmation = False
    risk = "low"

    if action in {"remove", "delete"}:
        requires_confirmation = True
        risk = "high"

    if action == "edit" and route in protected:
        requires_confirmation = True
        risk = "medium"

    if route in protected and action in {"remove", "delete"}:
        dangerous = True
        risk = "critical"
        requires_confirmation = True

    if route == "/" and action in {"remove", "delete"}:
        return MutationDecision(
            allowed=False,
            action=action,
            route=route,
            risk="critical",
            reason="Root route '/' cannot be deleted because it serves the route-directory homepage.",
            requires_confirmation=True,
            confirmed=confirmed,
            checks=checks,
        )

    stress_auto_confirmed = False

    if requires_confirmation and not confirmed:
        if can_auto_confirm_for_stress_test(
            action=action,
            route=route,
            protected=protected,
        ):
            confirmed = True
            stress_auto_confirmed = True
            checks.append({
                "name": "stress-test auto-confirmation allowed",
                "ok": True,
                "route": route,
                "action": action,
            })

    failed = [check for check in checks if not check.get("ok")]

    if failed:
        return MutationDecision(
            allowed=False,
            action=action,
            route=route,
            risk=risk,
            reason="Mutation decision failed safety checks: "
            + "; ".join(str(item.get("name")) for item in failed),
            requires_confirmation=requires_confirmation,
            confirmed=confirmed,
            checks=checks,
        )

    if requires_confirmation and not confirmed:
        return MutationDecision(
            allowed=False,
            action=action,
            route=route,
            risk=risk,
            reason="Mutation requires explicit confirmation before deployment.",
            requires_confirmation=True,
            confirmed=False,
            checks=checks,
        )

    if dangerous and confirmed:
        checks.append({
            "name": "dangerous protected-route mutation confirmed",
            "ok": True,
            "route": route,
        })

    reason = "Mutation allowed by decision policy."
    if stress_auto_confirmed:
        reason = "Mutation allowed by stress-test auto-confirmation policy."

    return MutationDecision(
        allowed=True,
        action=action,
        route=route,
        risk=risk,
        reason=reason,
        requires_confirmation=requires_confirmation,
        confirmed=confirmed,
        checks=checks,
    )


def enforce_mutation_decision(
    *,
    prompt: str,
    config: dict[str, Any] | None = None,
    confirmed: bool = False,
) -> MutationDecision:
    config = config if isinstance(config, dict) else load_current_config()

    decision = evaluate_mutation_decision(
        prompt=prompt,
        config=config,
        confirmed=confirmed,
    )

    audit_decision(decision, prompt)

    if not decision.allowed:
        print()
        print("🧠 Mutation decision blocked")
        print(f"Action: {decision.action}")
        print(f"Route: {decision.route}")
        print(f"Risk: {decision.risk}")
        print(f"Reason: {decision.reason}")

        if decision.requires_confirmation and not decision.confirmed:
            print()
            print("To continue intentionally, rerun with:")
            print("--confirm-route-mutation")

        raise SystemExit(2)

    print()
    print("🧠 Mutation decision allowed")
    print(f"Action: {decision.action}")
    print(f"Route: {decision.route}")
    print(f"Risk: {decision.risk}")
    print(f"Reason: {decision.reason}")

    return decision
