from __future__ import annotations

from core.agent_incident_commander import execute_repair_plan
from core.environment_repairs import repair_from_diagnosis, repair_public_edge


def test_repair_public_edge_preflight_allows_fresh_linux_host(monkeypatch):
    monkeypatch.setattr("core.environment_repairs.platform.system", lambda: "Linux")
    monkeypatch.setattr(
        "core.environment_repairs.shutil.which",
        lambda cmd: f"/usr/bin/{cmd}" if cmd in {"sudo", "apt", "systemctl"} else None,
    )

    calls = []

    def fake_run(cmd, timeout=60):
        calls.append((cmd, timeout))
        return {
            "cmd": cmd,
            "returncode": 0,
            "stdout": "",
            "stderr": "",
        }

    monkeypatch.setattr("core.environment_repairs._run", fake_run)

    result = repair_public_edge()

    assert result["ok"] is True
    assert calls[0][0] == ["sudo", "apt", "update"]
    assert calls[1][0][:4] == ["sudo", "apt", "install", "-y"]
    assert calls[1][1] == 180
    assert ["sudo", "nginx", "-t"] in [call[0] for call in calls]


def test_repair_public_edge_fails_closed_on_non_linux(monkeypatch):
    monkeypatch.setattr("core.environment_repairs.platform.system", lambda: "Darwin")

    result = repair_public_edge()

    assert result["ok"] is False
    assert result["action"] == "repair_public_edge"
    assert "requires Linux" in result["reason"]
    assert result["platform"] == "Darwin"


def test_execute_repair_plan_uses_registered_diagnosis_repair():
    incident = {
        "selected_root_cause": {
            "repair_action": "set_local_live_url_to_8090",
            "safe_to_auto_repair": True,
        },
        "config": {
            "live_url": "https://127.0.0.1:8443",
        },
    }

    result = execute_repair_plan(incident)

    assert result["ok"] is True
    assert result["config"]["live_url"] == "http://127.0.0.1:8090"
    assert incident["repair_result"] == result


def test_repair_from_diagnosis_sets_local_live_urls_without_mutating_input():
    config = {
        "live_url": "https://127.0.0.1:8443",
        "routes": [],
    }

    result = repair_from_diagnosis(
        {
            "repair_action": "set_local_live_url_to_8090",
            "safe_to_auto_repair": True,
        },
        config=config,
    )

    assert result["ok"] is True
    assert result["changed"] is True
    assert result["live_url"] == "http://127.0.0.1:8090"
    assert result["config"]["live_url"] == "http://127.0.0.1:8090"
    assert result["config"]["public_url"] == "http://127.0.0.1:8090"
    assert result["config"]["base_url"] == "http://127.0.0.1:8090"
    assert config["live_url"] == "https://127.0.0.1:8443"
    assert "public_url" not in config


def test_repair_from_diagnosis_rewrites_nested_bare_metal_upstreams():
    config = {
        "routes": [
            {
                "path": "/api",
                "upstream": "host.docker.internal:9101",
                "upstreams": [
                    "host.docker.internal:9101",
                    {"address": "host.docker.internal:9102", "weight": 2},
                ],
            }
        ],
        "note": "check host.docker.internal before deploy",
    }

    result = repair_from_diagnosis(
        {
            "repair_action": "normalize_bare_metal_upstreams",
            "safe_to_auto_repair": True,
        },
        config=config,
    )

    route = result["config"]["routes"][0]

    assert result["ok"] is True
    assert result["changed"] is True
    assert result["replacements"] == 4
    assert route["upstream"] == "127.0.0.1:9101"
    assert route["upstreams"][0] == "127.0.0.1:9101"
    assert route["upstreams"][1]["address"] == "127.0.0.1:9102"
    assert result["config"]["note"] == "check 127.0.0.1 before deploy"
    assert config["routes"][0]["upstream"] == "host.docker.internal:9101"


def test_repair_from_diagnosis_respects_safe_to_auto_repair_flag():
    result = repair_from_diagnosis(
        {
            "repair_action": "add_default_blocked_paths",
            "safe_to_auto_repair": False,
        },
        config={"security": {"blocked_paths": []}},
    )

    assert result["ok"] is False
    assert result["reason"] == "Diagnosis is not marked safe_to_auto_repair"


def test_repair_from_diagnosis_fails_closed_without_deployment_context():
    result = repair_from_diagnosis(
        {
            "repair_action": "upload_static_assets_to_release",
            "safe_to_auto_repair": True,
        },
        config={},
    )

    assert result["ok"] is False
    assert "deployment-specific" in result["reason"]
