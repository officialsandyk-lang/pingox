from __future__ import annotations

from copy import deepcopy


def find_route(config: dict, path: str) -> dict:
    for route in config["routes"]:
        if route.get("path") == path:
            return route
    raise AssertionError(f"Route not found: {path}")


def stable_config() -> dict:
    return {
        "port": 8090,
        "routes": [
            {
                "path": "/",
                "type": "static",
                "root": "public",
                "index": "index.html",
            },
            {
                "path": "/api",
                "upstream": "127.0.0.1:9101",
                "backend": "127.0.0.1:9101",
                "balancing": "random",
                "upstreams": [
                    {"address": "127.0.0.1:9101", "weight": 1},
                    {"address": "127.0.0.1:9102", "weight": 1},
                    {"address": "127.0.0.1:9103", "weight": 1},
                ],
            },
            {
                "path": "/users",
                "upstream": "127.0.0.1:9101",
                "backend": "127.0.0.1:9101",
                "balancing": "round_robin",
                "upstreams": [
                    {"address": "127.0.0.1:9101", "weight": 1},
                    {"address": "127.0.0.1:9102", "weight": 1},
                    {"address": "127.0.0.1:9103", "weight": 1},
                ],
            },
            {
                "path": "/orders",
                "upstream": "127.0.0.1:9201",
                "backend": "127.0.0.1:9201",
                "balancing": "weighted_round_robin",
                "upstreams": [
                    {"address": "127.0.0.1:9201", "weight": 5},
                    {"address": "127.0.0.1:9202", "weight": 2},
                    {"address": "127.0.0.1:9203", "weight": 1},
                ],
            },
            {
                "path": "/payments",
                "upstream": "127.0.0.1:9301",
                "backend": "127.0.0.1:9301",
                "balancing": "ip_hash",
                "upstreams": [
                    {"address": "127.0.0.1:9301", "weight": 1},
                    {"address": "127.0.0.1:9302", "weight": 1},
                ],
            },
            {
                "path": "/reports",
                "upstream": "127.0.0.1:9501",
                "backend": "127.0.0.1:9501",
                "balancing": "least_connections",
                "upstreams": [
                    {"address": "127.0.0.1:9501", "weight": 1},
                    {"address": "127.0.0.1:9502", "weight": 1},
                    {"address": "127.0.0.1:9503", "weight": 1},
                ],
            },
            {
                "path": "/inventory",
                "upstream": "127.0.0.1:9401",
                "backend": "127.0.0.1:9401",
                "upstreams": [
                    {"address": "127.0.0.1:9401", "weight": 1},
                ],
            },
        ],
        "security": {
            "allowed_methods": ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
            "blocked_paths": ["/.env", "/.git", "/admin.php", "/phpmyadmin", "/wp-admin", "/wp-login.php"],
            "rate_limit_per_minute": 10000,
            "max_connections": 1000,
            "max_request_body_bytes": 1048576,
            "upstream_timeout_seconds": 30,
        },
        "runtime": "local",
        "runtime_mode": "local",
        "strategy": "direct",
        "deployment_strategy": "direct",
    }


def test_merge_prompt_route_addition_preserves_existing_routes():
    from orchestration.update_graph import _merge_prompt_route_additions

    base = stable_config()
    before_api = deepcopy(find_route(base, "/api"))
    before_orders = deepcopy(find_route(base, "/orders"))
    before_payments = deepcopy(find_route(base, "/payments"))
    before_reports = deepcopy(find_route(base, "/reports"))

    updated, changes = _merge_prompt_route_additions(
        base,
        "add /search balanced across 127.0.0.1:9601, 127.0.0.1:9602, and 127.0.0.1:9603 using random",
    )

    assert "Added route /search" in changes

    assert find_route(updated, "/api") == before_api
    assert find_route(updated, "/orders") == before_orders
    assert find_route(updated, "/payments") == before_payments
    assert find_route(updated, "/reports") == before_reports

    search = find_route(updated, "/search")
    assert search["balancing"] == "random"
    assert search["upstream"] == "127.0.0.1:9601"
    assert search["upstreams"] == [
        "127.0.0.1:9601",
        "127.0.0.1:9602",
        "127.0.0.1:9603",
    ]


def test_run_update_graph_preserves_existing_routes_when_update_agent_mutates_them(monkeypatch, tmp_path):
    from orchestration import update_graph as ug

    base = stable_config()

    # Simulate the exact failure pattern:
    # config_update_agent adds/understands something but mutates old algorithms.
    mutated = deepcopy(base)
    find_route(mutated, "/api")["balancing"] = "round_robin"
    find_route(mutated, "/orders")["upstreams"] = [
        {"address": "127.0.0.1:9201", "weight": 1},
        {"address": "127.0.0.1:9202", "weight": 1},
        {"address": "127.0.0.1:9203", "weight": 1},
    ]
    find_route(mutated, "/payments")["balancing"] = "round_robin"
    find_route(mutated, "/reports")["balancing"] = "round_robin"

    class DummyReliability:
        def start_run(self, prompt: str, flow: str = "update") -> str:
            return "test_run"

        def finish_run(self, *args, **kwargs):
            return None

        def record_deployment_event(self, *args, **kwargs):
            return None

        def record_failure(self, *args, **kwargs):
            raise AssertionError("record_failure should not be called in this test")

    captured = {}

    monkeypatch.setattr(ug, "ReliabilityBrain", DummyReliability)
    monkeypatch.setattr(ug, "load_active_config", lambda: (deepcopy(base), "blue"))
    monkeypatch.setattr(ug, "_read_json", lambda path: None)

    monkeypatch.setattr(
        ug,
        "update_prompt_to_config",
        lambda active_config, prompt: {
            "config": deepcopy(mutated),
            "changed": False,
            "understood": True,
            "change_summary": ["Security unchanged: rate_limit_per_minute already 10000"],
        },
    )

    monkeypatch.setattr(ug, "run_config_repair", lambda config, prompt: config)
    monkeypatch.setattr(ug, "run_security", lambda config, prompt: config)
    monkeypatch.setattr(ug, "run_runtime_addressing", lambda config, runtime: config)
    monkeypatch.setattr(ug, "run_validator", lambda config: config)
    monkeypatch.setattr(ug, "run_config_preflight", lambda config, runtime, strategy: None)
    monkeypatch.setattr(ug, "run_project_writer", lambda config, project_dir: tmp_path)
    monkeypatch.setattr(ug, "run_cargo_check", lambda prompt, config, project_dir: None)
    monkeypatch.setattr(ug, "_save_latest_config", lambda config, active_color=None: None)

    def fake_deploy(*, config, project_dir, run_id=None):
        captured["config"] = deepcopy(config)
        return {
            "success": True,
            "deployed": True,
            "update_mode": "local_direct_staged",
            "active_color": "local",
            "live_url": "http://127.0.0.1:8090/",
            "restart_performed": True,
        }

    monkeypatch.setattr(ug, "run_local_direct_deploy", fake_deploy)

    result = ug.run_update_graph(
        "add /search balanced across 127.0.0.1:9601, 127.0.0.1:9602, and 127.0.0.1:9603 using random; direct update"
    )

    assert result["success"] is True
    assert result["deployed"] is True
    assert result["skipped_deploy"] is False

    deployed_config = captured["config"]

    assert find_route(deployed_config, "/api")["balancing"] == "random"
    assert [u["weight"] for u in find_route(deployed_config, "/orders")["upstreams"]] == [5, 2, 1]
    assert find_route(deployed_config, "/payments")["balancing"] == "ip_hash"
    assert find_route(deployed_config, "/reports")["balancing"] == "least_connections"

    search = find_route(deployed_config, "/search")
    assert search["balancing"] == "random"
    assert search["upstreams"] == [
        "127.0.0.1:9601",
        "127.0.0.1:9602",
        "127.0.0.1:9603",
    ]
