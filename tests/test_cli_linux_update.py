from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pingora_ai import cli


def _args(**overrides):
    values = {
        "host": "132.145.51.57",
        "user": "ubuntu",
        "ssh_key": "/tmp/test-key",
        "ssh_port": 22,
        "server_name": "_",
        "public_http_port": 80,
        "binary_path": "generated-pingora-proxy/target/release/generated-pingora-proxy-linux-amd64",
        "service_user": "ubuntu",
        "verify": "smoke",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _current_config():
    return {
        "port": 8080,
        "runtime": "bare_metal_linux",
        "strategy": "bluegreen",
        "routes": [
            {
                "path": "/",
                "type": "static",
                "root": "public",
                "index": "index.html",
                "upstreams": [],
            },
            {
                "path": "/api",
                "type": "proxy",
                "upstream": "127.0.0.1:9101",
                "balancing": "random",
                "upstreams": [
                    "127.0.0.1:9101",
                    "127.0.0.1:9102",
                    "127.0.0.1:9103",
                ],
            },
        ],
        "security": {
            "blocked_paths": ["/.env", "/.git"],
        },
    }


def test_linux_add_update_deploys_mutated_config(monkeypatch):
    deployed = {}

    monkeypatch.setattr(cli, "require_project_root", lambda: Path.cwd())
    monkeypatch.setattr(cli, "_load_current_gateway_config", lambda _root: _current_config())
    monkeypatch.setattr(cli, "_check_update_noop_or_conflict", lambda _prompt, _args: None)
    monkeypatch.setattr("core.mutation_decision.enforce_mutation_decision", lambda **_kwargs: None)
    monkeypatch.setattr("core.environment_deployer.create_backup", lambda reason: None)

    def fake_deploy(args, config):
        deployed["args"] = args
        deployed["config"] = config
        return 0

    monkeypatch.setattr(cli, "_deploy_bare_metal_from_cli", fake_deploy)

    result = cli.run_update_prompt(
        prompt="add /coupons to backend 127.0.0.1:9401",
        runtime="bare_metal_linux",
        strategy="bluegreen",
        verify="smoke",
        cli_args=_args(),
    )

    paths = {route["path"] for route in deployed["config"]["routes"]}

    assert result == 0
    assert "/coupons" in paths
    assert deployed["config"]["runtime"] == "bare_metal_linux"
    assert deployed["config"]["strategy"] == "bluegreen"


def test_linux_edit_update_deploys_algorithm_change(monkeypatch):
    deployed = {}

    monkeypatch.setattr(cli, "require_project_root", lambda: Path.cwd())
    monkeypatch.setattr(cli, "_load_current_gateway_config", lambda _root: _current_config())
    monkeypatch.setattr(cli, "_check_update_noop_or_conflict", lambda _prompt, _args: None)
    monkeypatch.setattr("core.mutation_decision.enforce_mutation_decision", lambda **_kwargs: None)
    monkeypatch.setattr("core.environment_deployer.create_backup", lambda reason: None)
    def fake_deploy(_args, config):
        deployed["config"] = config
        return 0

    monkeypatch.setattr(cli, "_deploy_bare_metal_from_cli", fake_deploy)

    result = cli.run_update_prompt(
        prompt="set /api algorithm to round_robin",
        runtime="bare_metal_linux",
        strategy="bluegreen",
        verify="smoke",
        confirm_route_mutation=True,
        cli_args=_args(confirm_route_mutation=True),
    )

    route = next(route for route in deployed["config"]["routes"] if route["path"] == "/api")

    assert result == 0
    assert route["balancing"] == "round_robin"
