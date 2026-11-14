from pathlib import Path
import yaml
from core.production import ProductionConfig, generate
from core.deployment_targets import DeploymentOptions


def test_runtime_mounts_preserve_atomic_replace(tmp_path):
    config = ProductionConfig.model_validate_json(Path('deploy/gateway.example.json').read_text())
    generate(config, tmp_path / 'compose')
    compose = yaml.safe_load((tmp_path / 'compose/compose.yaml').read_text())
    assert './runtime:/etc/pingox/runtime:ro' in compose['services']['gateway']['volumes']
    assert (tmp_path / 'compose/runtime/active.json').exists()
    generate(config, tmp_path / 'kube', DeploymentOptions(target='kubernetes', image='pingox:v2'))
    docs = list(yaml.safe_load_all((tmp_path / 'kube/kubernetes.yaml').read_text()))
    assert any(d['kind'] == 'ConfigMap' for d in docs)
    pod = next(d for d in docs if d['kind'] == 'Deployment')['spec']['template']['spec']
    assert all('subPath' not in mount for mount in pod['containers'][0]['volumeMounts'])


def test_tls_reaches_generated_peer_without_stripping(tmp_path):
    config = ProductionConfig.model_validate({'routes': [{'path': '/', 'upstreams': [
        {'address': 'backend.example:443', 'tls': True, 'server_name': 'backend.example'}]}]})
    generate(config, tmp_path)
    source = (tmp_path / 'src/main.rs').read_text()
    assert 'tls: true' in source
    assert 'server_name: "backend.example".to_string()' in source
    assert 'HttpPeer::new(address, upstream.tls, upstream.server_name.clone())' in source
