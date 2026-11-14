"""Mount a non-secret, externally replaceable configuration directory."""
from pathlib import Path
import yaml


def write_runtime(output: Path, config, options) -> None:
    directory = output / 'runtime'
    directory.mkdir(exist_ok=True)
    (directory / 'active.json').write_text(config.model_dump_json(indent=2) + '\n')
    if options.target == 'compose':
        path = output / 'compose.yaml'
        value = yaml.safe_load(path.read_text())
        gateway = value['services']['gateway']
        gateway['environment']['PINGOX_CONFIG_FILE'] = '/etc/pingox/runtime/active.json'
        gateway.setdefault('volumes', []).append('./runtime:/etc/pingox/runtime:ro')
        path.write_text(yaml.safe_dump(value, sort_keys=False))
    elif options.target == 'systemd':
        path = output / 'ai-pingora-gateway.service'
        path.write_text(path.read_text().replace('[Service]', '[Service]\nEnvironment=PINGOX_CONFIG_FILE=/opt/ai-pingora-gateway/current/runtime/active.json'))
    elif options.target == 'kubernetes':
        path = output / 'kubernetes.yaml'
        documents = list(yaml.safe_load_all(path.read_text()))
        deployment = next(d for d in documents if d['kind'] == 'Deployment')
        pod = deployment['spec']['template']['spec']
        pod.setdefault('volumes', []).append({'name': 'runtime-config', 'configMap': {'name': 'pingox-runtime'}})
        container = pod['containers'][0]
        container['env'].append({'name': 'PINGOX_CONFIG_FILE', 'value': '/etc/pingox/runtime/active.json'})
        container.setdefault('volumeMounts', []).append({'name': 'runtime-config', 'mountPath': '/etc/pingox/runtime', 'readOnly': True})
        documents.insert(1, {'apiVersion': 'v1', 'kind': 'ConfigMap',
                            'metadata': {'name': 'pingox-runtime', 'namespace': options.namespace},
                            'data': {'active.json': config.model_dump_json(indent=2)}})
        path.write_text(yaml.safe_dump_all(documents, sort_keys=False))
