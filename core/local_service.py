"""Native, offline service entrypoint. Service supervision belongs to the OS."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess

from core.production import ProductionConfig, generate
from core.deployment_targets import DeploymentOptions


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def build_release(config_path: Path, output: Path) -> Path:
    config = ProductionConfig.model_validate_json(config_path.read_text())
    cargo = shutil.which('cargo')
    if not cargo:
        raise RuntimeError('Rust/Cargo is required on the build machine')
    release = generate(config, output, DeploymentOptions(target='systemd'))
    # Fix the output location even when the caller uses a global Cargo target dir.
    result = subprocess.run([cargo, 'build', '--locked', '--release', '--target-dir',
                             str(release / 'target')], cwd=release)
    if result.returncode:
        raise RuntimeError('Native build failed; no runnable release was published')
    binary = release / 'gateway'
    shutil.copy2(release / 'target/release/generated-pingora-proxy', binary)
    binary.chmod(0o755)
    from core.license_notices import write_notices
    write_notices(release)
    manifest = {'version': 1, 'binary_sha256': digest(binary),
                'config_sha256': digest(release / 'production.json')}
    (release / 'native-release.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return release


def run_release(release: Path, bind: str = '127.0.0.1', state_dir: Path | None = None) -> None:
    host = str(ipaddress.IPv4Address(bind))
    release = release.resolve(strict=True)
    manifest = json.loads((release / 'native-release.json').read_text())
    binary = release / 'gateway'
    if (manifest.get('version') != 1
            or manifest.get('binary_sha256') != digest(binary)
            or manifest.get('config_sha256') != digest(release / 'production.json')):
        raise ValueError('Release changed after build; build a new release before running')
    # Replace Python so SIGTERM and exit status belong directly to Pingora.
    env = dict(os.environ, GATEWAY_BIND_HOST=host)
    if state_dir is not None:
        env['PINGOX_CONFIG_FILE'] = str((state_dir / 'active.json').resolve(strict=True))
    os.execve(str(binary), [str(binary)], env)
