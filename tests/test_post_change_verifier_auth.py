from __future__ import annotations

from core import environment_deployer
from core import post_change_verifier as verifier


def test_verifier_headers_prefers_deployed_nginx_api_key(tmp_path, monkeypatch):
    docker_dir = tmp_path / "docker-bluegreen"
    docker_dir.mkdir()
    (docker_dir / "nginx.conf").write_text(
        'if ($http_x_api_key = "active-edge-key") { set $api_key_valid 1; }',
        encoding="utf-8",
    )

    monkeypatch.setattr(verifier, "_candidate_project_roots", lambda: [tmp_path])
    monkeypatch.setenv("PINGORA_EDGE_API_KEY", "stale-env-key")

    assert verifier._verifier_headers()["X-API-Key"] == "active-edge-key"


def test_verifier_headers_reads_env_file_when_no_deployed_nginx_key(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        'PINGORA_EDGE_API_KEY="env-file-key"\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(verifier, "_candidate_project_roots", lambda: [tmp_path])
    monkeypatch.delenv("PINGORA_EDGE_API_KEY", raising=False)

    assert verifier._verifier_headers()["X-API-Key"] == "env-file-key"


def test_docker_nginx_writer_reads_env_file_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("PINGORA_EDGE_API_KEY", raising=False)
    monkeypatch.setattr(environment_deployer, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(environment_deployer, "DOCKER_DIR", tmp_path / "docker-bluegreen")

    (tmp_path / ".env").write_text(
        'PINGORA_EDGE_API_KEY="env-file-key"\n',
        encoding="utf-8",
    )

    environment_deployer.write_nginx_conf("blue", 8090)

    nginx_conf = (tmp_path / "docker-bluegreen" / "nginx.conf").read_text(encoding="utf-8")
    assert 'if ($http_x_api_key = "env-file-key")' in nginx_conf
