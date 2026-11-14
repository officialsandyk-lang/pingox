from pathlib import Path
import subprocess
import sys
import pytest
import yaml
from pydantic import ValidationError
from core.production import ProductionConfig, generate
from core.deployment_targets import DeploymentOptions


@pytest.mark.parametrize('target', ['compose', 'container', 'systemd', 'kubernetes'])
def test_authentication_uses_runtime_secrets(tmp_path, target):
    config = ProductionConfig.model_validate_json(Path('deploy/gateway.example.json').read_text())
    config.security.require_api_key = True
    options = DeploymentOptions(target=target, image='pingox:v1' if target == 'kubernetes' else None)
    generate(config, tmp_path, options)
    assert (tmp_path / 'AUTHENTICATION.md').exists()
    assert 'require_api_key: true' in (tmp_path / 'src/main.rs').read_text()
    assert not (tmp_path / 'api-keys.json').exists()
    if target == 'compose':
        value = yaml.safe_load((tmp_path / 'compose.yaml').read_text())
        assert value['services']['gateway']['secrets'] == ['pingox_api_keys']
        assert '${PINGOX_API_KEYS_FILE:?' in value['secrets']['pingox_api_keys']['file']
    if target == 'systemd':
        assert 'LoadCredential=api_keys:' in (tmp_path / 'ai-pingora-gateway.service').read_text()
    if target == 'kubernetes':
        docs = list(yaml.safe_load_all((tmp_path / 'kubernetes.yaml').read_text()))
        pod = next(d for d in docs if d['kind'] == 'Deployment')['spec']['template']['spec']
        assert pod['securityContext']['fsGroup'] == 65532
        assert pod['volumes'][0]['secret']['defaultMode'] == 0o440
        assert not any(d['kind'] == 'Secret' for d in docs)


@pytest.mark.parametrize('field,value', [('failure_threshold', 0), ('failure_cooldown_seconds', 0),
                                         ('failure_cooldown_seconds', 301), ('require_api_key', 'yes')])
def test_invalid_reliability_policy_rejected(field, value):
    config = ProductionConfig.model_validate_json(Path('deploy/gateway.example.json').read_text()).model_dump()
    config['security'][field] = value
    with pytest.raises(ValidationError):
        ProductionConfig.model_validate(config)


def test_pingox_command_entrypoint():
    result = subprocess.run([sys.executable, 'pingox.py', 'validate', '--config', 'deploy/gateway.example.json'], capture_output=True, text=True)
    assert result.returncode == 0 and 'valid' in result.stdout
