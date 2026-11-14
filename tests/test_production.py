import copy
import json
from pathlib import Path
import pytest
import yaml
from pydantic import ValidationError
from core.production import ProductionConfig, generate


def example():
    return json.loads(Path("deploy/gateway.example.json").read_text())


@pytest.mark.parametrize("address", ["https://example.com:443", "api:70000", "api:0", "api:80/private", "api;echo:80"])
def test_bad_upstreams_fail_instead_of_silent_repair(address):
    data = example()
    data["routes"][0]["upstreams"][0]["address"] = address
    with pytest.raises(ValidationError):
        ProductionConfig.model_validate(data)


@pytest.mark.parametrize("path", ["/api/../admin", "/api%2fadmin", "/api//admin", "/_gateway/health"])
def test_ambiguous_or_reserved_paths_fail(path):
    data = example()
    data["routes"][0]["path"] = path
    with pytest.raises(ValidationError):
        ProductionConfig.model_validate(data)


def test_weighted_pool_preserved_and_deploy_has_no_demo(tmp_path):
    data = example()
    data["routes"][0]["balancing"] = "weighted_round_robin"
    data["routes"][0]["upstreams"] = [{"address": "api-a:8000", "weight": 5}, {"address": "api-b:8000", "weight": 2}]
    generate(ProductionConfig.model_validate(data), tmp_path)
    config = json.loads((tmp_path / "config.json").read_text())
    assert config["routes"][0]["upstreams"] == ProductionConfig.model_validate(data).model_dump()["routes"][0]["upstreams"]
    compose = yaml.safe_load((tmp_path / "compose.yaml").read_text())
    assert set(compose["services"]) == {"gateway", "edge"}
    gateway = compose["services"]["gateway"]
    assert gateway["read_only"] and gateway["cap_drop"] == ["ALL"]
    assert "ports" not in gateway
    assert "--locked" in (tmp_path / "Dockerfile").read_text()
    assert not list(tmp_path.glob("*backend*"))
    with pytest.raises(ValueError, match="empty"):
        generate(ProductionConfig.model_validate(data), tmp_path)


def test_unknown_fields_and_duplicate_routes_fail():
    data = example()
    data["routes"].append(copy.deepcopy(data["routes"][0]))
    with pytest.raises(ValidationError): ProductionConfig.model_validate(data)
    data = example()
    data["security"]["max_conections"] = 10
    with pytest.raises(ValidationError): ProductionConfig.model_validate(data)


def test_legacy_compose_handles_static_routes_and_weighted_pools():
    from core.compose_writer import build_compose_routes, get_backend_ports
    config = {"routes": [
        {"path": "/", "type": "static", "root": "public"},
        {"path": "/api", "balancing": "weighted_round_robin", "upstreams": [
            {"address": "127.0.0.1:9001", "weight": 5},
            {"address": "127.0.0.1:9002", "weight": 2}]}]}
    routes = build_compose_routes(config)
    assert get_backend_ports(config) == [9001, 9002]
    assert routes[0]["type"] == "static"
    assert routes[1]["upstreams"] == [
        {"address": "backend-9001:9001", "weight": 5},
        {"address": "backend-9002:9002", "weight": 2}]


@pytest.mark.parametrize("route", [
    {"path": "/", "type": "static", "root": "../outside"},
    {"path": "/", "type": "static", "root": "public", "index": "../../outside.txt"},
])
def test_generated_static_files_cannot_escape_output(tmp_path, route):
    from core.project_writer import write_project
    with pytest.raises(ValueError, match="escapes"):
        write_project({"port": 8080, "routes": [route]}, tmp_path / "release")
    assert not (tmp_path / "outside").exists()
    assert not (tmp_path / "outside.txt").exists()
