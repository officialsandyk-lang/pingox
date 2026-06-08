from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.decision_engine import (
    apply_deterministic_repairs,
    decide_mutation_safety,
    evaluate_config,
)


def assert_ok(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"✅ {name}")


def main() -> int:
    bad_weighted = {
        "runtime": "local",
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
            }
        ],
    }

    result = evaluate_config(bad_weighted)
    assert_ok("bad weighted config is rejected", result["ok"] is False)

    categories = {item["category"] for item in result["failed_decisions"]}
    assert_ok("weighted failure is classified", "weighted_round_robin_integrity" in categories)

    repaired = apply_deterministic_repairs(bad_weighted, runtime="local")
    repaired_result = evaluate_config(repaired)

    # Repair can remove duplicate/mixed shape, but cannot infer original lost 5:2:1 weights.
    assert_ok("unknown lost weights still rejected after repair", repaired_result["ok"] is False)

    good_weighted = {
        "runtime": "local",
        "strategy": "direct",
        "routes": [
            {
                "path": "/",
                "type": "static",
                "route_type": "static",
                "upstreams": [],
            },
            {
                "path": "/orders",
                "balancing": "weighted_round_robin",
                "upstreams": [
                    {"address": "127.0.0.1:9201", "weight": 5},
                    {"address": "127.0.0.1:9202", "weight": 2},
                    {"address": "127.0.0.1:9203", "weight": 1},
                ],
            },
        ],
    }

    assert_ok("good weighted config passes", evaluate_config(good_weighted)["ok"] is True)

    docker_fixed = apply_deterministic_repairs(good_weighted, runtime="docker_host")
    docker_text = json.dumps(docker_fixed)

    assert_ok("docker repair rewrites to host.docker.internal", "host.docker.internal:9201" in docker_text)
    assert_ok("docker repair removes 127.0.0.1", "127.0.0.1:9201" not in docker_text)

    assert_ok(
        "edit without confirmation is denied",
        decide_mutation_safety("set /api algorithm to random").ok is False,
    )

    assert_ok(
        "edit with confirmation is allowed",
        decide_mutation_safety("set /api algorithm to random CONFIRM ROUTE MUTATION").ok is True,
    )

    print("")
    print("✅ DECISION ENGINE STRESS TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
