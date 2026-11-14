import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from core.local_service import digest, run_release, build_release


def fixture_release(path):
    binary = path / 'gateway'
    binary.write_text('#!/bin/sh\nprintf "%s" "$GATEWAY_BIND_HOST"\nexit 23\n')
    binary.chmod(0o755)
    config = path / 'production.json'
    config.write_text('{}')
    (path / 'native-release.json').write_text(json.dumps({
        'version': 1, 'binary_sha256': digest(binary), 'config_sha256': digest(config)}))


def test_run_replaces_process_and_preserves_exit_status(tmp_path):
    fixture_release(tmp_path)
    result = subprocess.run([sys.executable, 'gateway.py', 'run', '--release', str(tmp_path)],
                            capture_output=True, text=True,
                            env=dict(os.environ, GATEWAY_BIND_HOST='0.0.0.0', AI_WORKER_BACKEND='disabled'))
    assert result.returncode == 23
    assert result.stdout == '127.0.0.1'


@pytest.mark.parametrize('filename', ['gateway', 'production.json'])
def test_modified_release_cannot_start(tmp_path, filename):
    fixture_release(tmp_path)
    with (tmp_path / filename).open('a') as f:
        f.write('changed')
    with pytest.raises(ValueError, match='Release changed'):
        run_release(tmp_path)


def test_invalid_bind_rejected_before_execution(tmp_path):
    fixture_release(tmp_path)
    with pytest.raises(ValueError):
        run_release(tmp_path, 'example.com')


def test_failed_build_has_no_runnable_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr('core.local_service.shutil.which', lambda _: '/fake/cargo')
    monkeypatch.setattr('core.local_service.subprocess.run', lambda *a, **k: subprocess.CompletedProcess(a, 1))
    output = tmp_path / 'release'
    with pytest.raises(RuntimeError, match='Native build failed'):
        build_release(Path('deploy/gateway.example.json'), output)
    assert not (output / 'native-release.json').exists()
