"""Runtime secret wiring; credentials are never read during generation."""
from pathlib import Path
import yaml


def write_credentials(output: Path, target: str) -> None:
    instructions = '''# Pingox API keys

Authentication is enabled. Supply a JSON array containing 1–64 independently
random 64-character hex keys. Generate each with `openssl rand -hex 32`.
Keep the file outside this release/build directory, with restricted permissions.
Send a key in `X-Pingox-Api-Key` over HTTPS. The gateway strips that header before
proxying. Keys are loaded at startup; restart after rotating the file. Keep old
and new keys during a planned overlap, then remove the old key and restart.
Do not commit keys. This provides shared-key admission, not user identities,
scopes, JWT validation, per-consumer quotas or application authorization.
Health/readiness/metrics remain internal unauthenticated endpoints: block the
entire /_gateway path at public ingress, as the generated Caddy config does.

'''
    if target == 'compose':
        path = output / 'compose.yaml'
        compose = yaml.safe_load(path.read_text())
        gateway = compose['services']['gateway']
        gateway['environment']['PINGOX_API_KEYS_FILE'] = '/run/secrets/pingox_api_keys'
        gateway['secrets'] = ['pingox_api_keys']
        compose['secrets'] = {'pingox_api_keys': {'file': '${PINGOX_API_KEYS_FILE:?Set the absolute API key file path}'}}
        path.write_text(yaml.safe_dump(compose, sort_keys=False))
        with (output / '.env.example').open('a') as f:
            f.write('PINGOX_API_KEYS_FILE=\n')
        instructions += ('Set PINGOX_API_KEYS_FILE in .env to the host file. Local Compose secret\n'
                         'mounts preserve host permissions: make it readable by container UID 65532\n'
                         '(for example owner 65532, mode 0400), without world-readable permissions.\n')
    elif target == 'systemd':
        path = output / 'ai-pingora-gateway.service'
        path.write_text(path.read_text().replace('[Service]', '[Service]\nLoadCredential=api_keys:/etc/pingox/api-keys.json\nEnvironment=PINGOX_API_KEYS_FILE=%d/api_keys'))
        instructions += ('Place the root-owned key file at /etc/pingox/api-keys.json with mode 0600.\n'
                         'systemd LoadCredential makes it available to the isolated service user.\n')
    elif target == 'kubernetes':
        path = output / 'kubernetes.yaml'
        docs = list(yaml.safe_load_all(path.read_text()))
        deployment = next(d for d in docs if d['kind'] == 'Deployment')
        pod = deployment['spec']['template']['spec']
        pod['securityContext']['fsGroup'] = 65532
        pod['volumes'] = [{'name': 'api-keys', 'secret': {'secretName': 'pingox-api-keys', 'defaultMode': 0o440}}]
        container = pod['containers'][0]
        container['env'].append({'name': 'PINGOX_API_KEYS_FILE', 'value': '/run/secrets/pingox/keys.json'})
        container['volumeMounts'] = [{'name': 'api-keys', 'mountPath': '/run/secrets/pingox', 'readOnly': True}]
        path.write_text(yaml.safe_dump_all(docs, sort_keys=False))
        instructions += ('Create Secret pingox-api-keys in the deployment namespace with key keys.json\n'
                         'containing the JSON array before starting the deployment. The manifest\n'
                         'references the secret but never contains its contents. Restart the rollout\n'
                         'after rotation. Restrict access to Secrets and enable encryption at rest.\n')
    else:
        instructions += ('Mount the key file read-only at /run/secrets/pingox/keys.json, readable by\n'
                         'container UID 65532. Set PINGOX_API_KEYS_FILE to that container path.\n')
    (output / 'AUTHENTICATION.md').write_text(instructions)
