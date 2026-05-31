from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.system_decision_engine import (
    apply_system_repairs,
    evaluate_system_config,
)


def ok(name: str, condition: bool):
    if not condition:
        raise AssertionError(name)
    print(f"✅ {name}")


def categories(result):
    return {item["category"] for item in result["failed_decisions"]}


def main() -> int:
    bad = {
        "runtime": "docker_host",
        "strategy": "direct",
        "routes": [
            {
                "path": "/orders",
                "balancing": "weighted_round_robin",
                "upstreams": [
                    "127.0.0.1:9201",
                    {"upstream": "127.0.0.1:9201", "weight": 1},
                    {"upstream": "127.0.0.1:9202", "weight": 1},
                    {"upstream": "127.0.0.1:9203", "weight": 1},
                ],
            },
            {
                "path": "/api",
                "balancing": "random",
                "upstreams": ["127.0.0.1:9101", "127.0.0.1:9102"],
            },
        ],
    }

    result = evaluate_system_config(
        bad,
        prompt="set /api algorithm to random",
        strict_security=True,
    )

    cats = categories(result)

    ok("bad config rejected", result["ok"] is False)
    ok("runtime strategy checked", "runtime_strategy" in cats)
    ok("weighted algorithm checked", "weighted_round_robin_integrity" in cats)
    ok("health check checked", "health_check" in cats)
    ok("runtime addressing checked", "runtime_addressing" in cats)
    ok("security contract checked", "security_contract" in cats)
    ok("mutation safety checked", "route_mutation_safety" in cats)

    repaired = apply_system_repairs(bad)
    repaired_result = evaluate_system_config(
        repaired,
        prompt="add /x to backend 127.0.0.1:9000",
        strict_security=False,
    )

    ok("repair does not invent lost weighted intent", repaired_result["ok"] is False)
    ok("weighted corruption remains blocked after repair", "weighted_round_robin_integrity" in categories(repaired_result))

    good = {
        "runtime": "local",
        "strategy": "direct",
        "routes": [
            {"path": "/", "type": "static", "route_type": "static", "upstreams": []},
            {
                "path": "/api",
                "balancing": "random",
                "health_check": {
                    "enabled": True,
                    "path": "/health",
                    "interval_seconds": 5,
                    "timeout_seconds": 2,
                    "healthy_threshold": 2,
                    "unhealthy_threshold": 3,
                },
                "upstreams": ["127.0.0.1:9101", "127.0.0.1:9102", "127.0.0.1:9103"],
            },
            {
                "path": "/orders",
                "balancing": "weighted_round_robin",
                "health_check": {
                    "enabled": True,
                    "path": "/health",
                    "interval_seconds": 5,
                    "timeout_seconds": 2,
                    "healthy_threshold": 2,
                    "unhealthy_threshold": 3,
                },
                "upstreams": [
                    {"address": "127.0.0.1:9201", "weight": 5},
                    {"address": "127.0.0.1:9202", "weight": 2},
                    {"address": "127.0.0.1:9203", "weight": 1},
                ],
            },
        ],
    }

    good_result = evaluate_system_config(
        good,
        prompt="add /x to backend 127.0.0.1:9000",
        strict_security=False,
    )
    ok("good config accepted", good_result["ok"] is True)

    print("")
    print("✅ SYSTEM DECISION ENGINE STRESS TEST PASSED")
    print("✅ runtime")
    print("✅ strategy")
    print("✅ addressing")
    print("✅ routes")
    print("✅ algorithms")
    print("✅ health checks")
    print("✅ security contract checks")
    print("✅ mutation safety")
    print("✅ deterministic repair limits")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
