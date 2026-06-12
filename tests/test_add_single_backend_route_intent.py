from agents.config_update_agent import apply_config_update
from core.system_decision_engine import apply_and_enforce_system_decisions


def test_add_single_backend_route_stays_single_not_round_robin():
    base = {
        "runtime": "local",
        "runtime_mode": "local",
        "strategy": "direct",
        "deployment_strategy": "direct",
        "listen_port": 8080,
        "routes": [
            {
                "path": "/",
                "type": "static",
                "balancing": "single",
                "upstreams": [],
            },
            {
                "path": "/api",
                "balancing": "random",
                "upstreams": ["127.0.0.1:9101", "127.0.0.1:9102"],
            },
        ],
    }

    result = apply_config_update(base, "add /users to backend 127.0.0.1:9101")
    updated = result["config"]

    users_before_decision = next(route for route in updated["routes"] if route.get("path") == "/users")
    assert users_before_decision.get("balancing") in {None, "single"}
    assert users_before_decision.get("upstream") == "127.0.0.1:9101"
    before_upstreams = users_before_decision.get("upstreams")
    assert before_upstreams is None or before_upstreams == ["127.0.0.1:9101"]

    decided = apply_and_enforce_system_decisions(updated)
    users_after_decision = next(route for route in decided["routes"] if route.get("path") == "/users")

    assert users_after_decision.get("balancing") == "single"
    assert users_after_decision.get("upstreams") == ["127.0.0.1:9101"]
    assert users_after_decision.get("balancing") != "round_robin"
