from __future__ import annotations

import subprocess
from types import SimpleNamespace

from core.bare_metal.deployer import BareMetalLinuxDeployer
from core.bare_metal.state import color_port, default_state


class FakeSSH:
    def __init__(self):
        self.calls: list[dict[str, object]] = []

    def run(self, command, *, sudo=False, check=True, timeout=120):
        self.calls.append(
            {
                "command": command,
                "sudo": sudo,
                "check": check,
                "timeout": timeout,
            }
        )

        if "systemctl stop" in command:
            raise subprocess.TimeoutExpired(command, timeout)

        return subprocess.CompletedProcess(command, 0, "", "")


def test_stop_service_is_bounded_and_tolerates_systemd_stop_timeout():
    deployer = object.__new__(BareMetalLinuxDeployer)
    deployer.ssh = FakeSSH()

    deployer._stop_service("green")

    commands = [str(call["command"]) for call in deployer.ssh.calls]

    assert commands[0] == "systemctl kill --kill-who=all -s SIGKILL pingora-ai-green"
    assert "pkill -9 -f '/opt/pingora-ai/releases/[g]reen/pingora-gateway'" in commands
    assert "systemctl stop --no-block pingora-ai-green" in commands
    assert commands[-1] == "systemctl reset-failed pingora-ai-green"

    stop_call = next(call for call in deployer.ssh.calls if "systemctl stop" in str(call["command"]))
    assert stop_call["check"] is False
    assert stop_call["timeout"] == 5


def test_stop_old_service_uses_same_bounded_cleanup(monkeypatch):
    deployer = object.__new__(BareMetalLinuxDeployer)
    stopped = []

    monkeypatch.setattr(deployer, "_stop_service", stopped.append)

    deployer._stop_old_service("blue")

    assert stopped == ["blue"]


def test_preflight_removes_stale_default_nginx_sites():
    deployer = object.__new__(BareMetalLinuxDeployer)
    deployer.ssh = FakeSSH()
    deployer.options = SimpleNamespace(user="ubuntu", service_user="ubuntu")

    deployer._preflight()

    remove_commands = [
        str(call["command"])
        for call in deployer.ssh.calls
        if str(call["command"]).startswith("rm -f ")
    ]

    assert len(remove_commands) == 1
    assert "/etc/nginx/sites-enabled/default" in remove_commands[0]
    assert "/etc/nginx/sites-available/default" in remove_commands[0]
    assert "/etc/nginx/sites-enabled/pingora-ai-edge" in remove_commands[0]
    assert "/etc/nginx/sites-available/pingora-ai-edge" in remove_commands[0]


def test_bare_metal_bluegreen_colors_use_distinct_ports():
    assert color_port("green") == 8090
    assert color_port("blue") == 8091

    state = default_state()

    assert state.active_color == "green"
    assert state.active_port == 8090


def test_bare_metal_health_gate_checks_active_deploy_port(monkeypatch):
    from core.environment_deployer import _run_bare_metal_health_gate

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = _run_bare_metal_health_gate(
        {
            "success": True,
            "active_port": 8091,
        },
        host="132.145.51.57",
        user="ubuntu",
        ssh_key="/tmp/test-key",
        ssh_port=22,
    )

    remote_script = captured["cmd"][-1]

    assert result["success"] is True
    assert "http://127.0.0.1:8091/api/" in remote_script
    assert "http://127.0.0.1:8091/__gateway_runtime_state" in remote_script
    assert "http://127.0.0.1:8090/api/" not in remote_script


def test_bare_metal_binary_auto_build_uses_docker_and_writes_requested_path(monkeypatch, tmp_path):
    from core import environment_deployer

    output = tmp_path / "generated-pingora-proxy-linux-amd64"
    calls = []

    monkeypatch.setattr(environment_deployer.shutil, "which", lambda cmd: "/usr/bin/docker" if cmd == "docker" else None)
    monkeypatch.setattr(environment_deployer, "write_dockerfile", lambda: None)

    def fake_run(command, **kwargs):
        calls.append(command)

        if command[:2] == ["docker", "cp"]:
            output.write_text("binary", encoding="utf-8")

        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(environment_deployer, "run_command", fake_run)

    result = environment_deployer._build_bare_metal_linux_binary(
        output,
        auto_build_binary=True,
    )

    assert result["success"] is True
    assert result["binary_path"] == str(output)
    assert output.exists()
    assert calls[0][:4] == ["docker", "build", "--platform", "linux/amd64"]
    assert calls[1][:3] == ["docker", "create", "--name"]
    assert calls[2][:2] == ["docker", "cp"]
    assert calls[3][:3] == ["docker", "rm", "-f"]


def test_bare_metal_prepare_preserves_structured_upstreams():
    from core.environment_deployer import _prepare_bare_metal_pipeline_config

    config = {
        "routes": [
            {
                "path": "/api",
                "type": "proxy",
                "upstream": "host.docker.internal:9101",
                "upstreams": [
                    {"address": "host.docker.internal:9101", "weight": 2},
                    {"address": "127.0.0.1:9102", "weight": 1},
                ],
            },
            {
                "path": "/coupons",
                "upstream": "127.0.0.1:9401",
            },
        ]
    }

    prepared = _prepare_bare_metal_pipeline_config(
        config,
        host="132.145.51.57",
        public_http_port=80,
    )

    route = next(route for route in prepared["routes"] if route["path"] == "/api")

    assert route["upstream"] == "127.0.0.1:9101"
    assert route["upstreams"] == [
        {"address": "127.0.0.1:9101", "weight": 2},
        {"address": "127.0.0.1:9102", "weight": 1},
    ]

    coupons = next(route for route in prepared["routes"] if route["path"] == "/coupons")
    assert coupons["type"] == "proxy"
    assert coupons["route_type"] == "proxy"
    assert coupons["health_check"]["path"] == "/health"
