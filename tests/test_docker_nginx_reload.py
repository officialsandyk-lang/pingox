from __future__ import annotations

import subprocess


def test_nginx_reload_uses_default_running_config(monkeypatch, tmp_path):
    from core import environment_deployer

    commands: list[list[str]] = []

    monkeypatch.setattr(environment_deployer, "DOCKER_DIR", tmp_path)
    monkeypatch.setattr(environment_deployer, "validate_nginx_file", lambda _path: None)

    (tmp_path / "nginx.conf").write_text("events {}\n", encoding="utf-8")

    def fake_run_command(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(environment_deployer, "run_command", fake_run_command)

    result = environment_deployer.reload_nginx_edge()

    assert result.returncode == 0
    assert [
        "docker",
        "exec",
        "ai-pingora-gateway-edge",
        "nginx",
        "-t",
    ] in commands
    assert commands[-1] == [
        "docker",
        "exec",
        "ai-pingora-gateway-edge",
        "nginx",
        "-s",
        "reload",
    ]
