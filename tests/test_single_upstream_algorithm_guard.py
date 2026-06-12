import pytest

from core.system_decision_engine import apply_and_enforce_system_decisions


@pytest.mark.parametrize(
    "algorithm",
    ["round_robin", "random", "least_connections", "ip_hash", "weighted_round_robin"],
)
def test_multi_backend_algorithms_reject_single_upstream(algorithm):
    config = {
        "routes": [
            {
                "path": "/users",
                "balancing": algorithm,
                "algorithm": algorithm,
                "upstreams": ["127.0.0.1:9101"],
            }
        ]
    }

    with pytest.raises(RuntimeError) as exc:
        apply_and_enforce_system_decisions(config)

    message = str(exc.value)
    assert "System decision engine rejected config" in message
    assert "route has only 1 upstream" in message


def test_single_algorithm_allows_single_upstream():
    config = {
        "routes": [
            {
                "path": "/users",
                "balancing": "single",
                "algorithm": "single",
                "upstreams": ["127.0.0.1:9101"],
            }
        ]
    }

    result = apply_and_enforce_system_decisions(config)
    users_route = next((route for route in result["routes"] if route.get("path") == "/users"), None)
    assert users_route is not None
    assert users_route.get("balancing") in {"single", None}
