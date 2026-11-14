import json
from pathlib import Path
import subprocess
import sys
import pytest
import yaml
from pydantic import ValidationError
from core.production import ProductionConfig, generate
from core.deployment_targets import DeploymentOptions


def config():
    return ProductionConfig.model_validate_json(Path('deploy/gateway.example.json').read_text())


def test_kubernetes_manifest_security_and_rollout(tmp_path):
    generate(config(), tmp_path, DeploymentOptions(target='kubernetes', image='registry.example/gateway:v1'))
    resources = list(yaml.safe_load_all((tmp_path / 'kubernetes.yaml').read_text()))
    deployment = next(r for r in resources if r['kind'] == 'Deployment')
    spec = deployment['spec']['template']['spec']
    assert spec['automountServiceAccountToken'] is False
    assert spec['securityContext']['runAsNonRoot'] is True
    assert spec['containers'][0]['securityContext']['readOnlyRootFilesystem'] is True
    assert deployment['spec']['strategy']['rollingUpdate']['maxUnavailable'] == 0
    assert next(r for r in resources if r['kind'] == 'Service')['spec']['type'] == 'ClusterIP'
    assert not (tmp_path / 'compose.yaml').exists()


@pytest.mark.parametrize('kwargs', [
    {'target': 'kubernetes'}, {'target': 'kubernetes', 'image': 'gateway:latest'},
    {'target': 'kubernetes', 'image': 'gateway:v1', 'namespace': '../bad'},
    {'target': 'systemd', 'trusted_proxy_cidrs': ['0.0.0.0/0']},
])
def test_invalid_profiles_fail_before_writing(tmp_path, kwargs):
    with pytest.raises(ValidationError): DeploymentOptions(**kwargs)
    assert not list(tmp_path.iterdir())


def test_systemd_loopback_and_hardening(tmp_path):
    value = config().model_dump()
    value['routes'][0]['upstreams'][0]['address'] = '127.0.0.1:8000'
    c = ProductionConfig.model_validate(value)
    with pytest.raises(ValueError, match='loopback'):
        generate(c, tmp_path / 'bad', DeploymentOptions(target='container'))
    assert not (tmp_path / 'bad').exists()
    generate(c, tmp_path / 'good', DeploymentOptions(target='systemd'))
    unit = (tmp_path / 'good/ai-pingora-gateway.service').read_text()
    for expected in ['DynamicUser=yes', 'ProtectSystem=strict', 'GATEWAY_BIND_HOST=127.0.0.1', 'NoNewPrivileges=yes']:
        assert expected in unit


def test_init_and_generate_cli(tmp_path):
    root = tmp_path / 'project'
    subprocess.run([sys.executable, 'gateway.py', 'init', '--directory', str(root), '--target', 'kubernetes', '--image', 'gateway:v1'], check=True)
    subprocess.run([sys.executable, 'gateway.py', 'generate', '--config', str(root/'gateway.json'), '--deployment', str(root/'deployment.json'), '--output', str(root/'release')], check=True)
    assert (root/'release/kubernetes.yaml').exists()


def test_cli_validation_errors_do_not_print_secret_values(tmp_path):
    path = tmp_path / 'invalid.json'
    value = config().model_dump()
    value['api_key'] = 'VERY_SECRET_MUST_NOT_APPEAR'
    path.write_text(json.dumps(value))
    result = subprocess.run([sys.executable, 'gateway.py', 'validate', '--config', str(path)], capture_output=True, text=True)
    assert result.returncode != 0
    assert 'VERY_SECRET' not in result.stderr
    assert 'api_key' in result.stderr


def test_compose_profile_image_is_used(tmp_path):
    generate(config(), tmp_path, DeploymentOptions(target='compose', image='registry.example/gateway:v7'))
    assert 'GATEWAY_IMAGE=registry.example/gateway:v7' in (tmp_path/'.env.example').read_text()


def test_target_inapplicable_options_are_rejected():
    with pytest.raises(ValidationError): DeploymentOptions(target='systemd', image='gateway:v1')
    with pytest.raises(ValidationError): DeploymentOptions(target='compose', replicas=5)
