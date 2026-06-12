from __future__ import annotations

from argparse import Namespace

from pingora_ai.cli import repair_planner_config_from_prompt


def test_repair_planner_config_preserves_multi_upstream_prompt_intent():
    args = Namespace(
        local=False,
        docker=True,
        linux=False,
        runtime="docker_host",
        strategy="bluegreen",
    )
    config = {
        "routes": [
            {
                "path": "/api",
                "upstreams": ["127.0.0.1:3000"],
                "upstream": "127.0.0.1:3000",
                "balancing": "random",
            }
        ],
    }
    prompt = (
        "create gateway with /api random across "
        "127.0.0.1:9101, 127.0.0.1:9102 and 127.0.0.1:9103"
    )

    repaired = repair_planner_config_from_prompt(config, args, prompt)

    api_route = next(route for route in repaired["routes"] if route.get("path") == "/api")

    assert api_route["balancing"] == "random"
    assert api_route["upstreams"] == [
        "127.0.0.1:9101",
        "127.0.0.1:9102",
        "127.0.0.1:9103",
    ]
    assert api_route["upstream"] == "127.0.0.1:9101"
    assert repaired["runtime"] == "docker_host"
    assert repaired["strategy"] == "bluegreen"
