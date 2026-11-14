from pathlib import Path
import hashlib
import io
import json
import subprocess
import tarfile
import pytest
from core.release_bundle import package, install, rollback


@pytest.fixture
def signing(tmp_path):
    secret, public = tmp_path / 'private.pem', tmp_path / 'public.pem'
    subprocess.run(['openssl', 'genpkey', '-algorithm', 'ED25519', '-out', str(secret)], check=True, capture_output=True)
    subprocess.run(['openssl', 'pkey', '-in', str(secret), '-pubout', '-out', str(public)], check=True, capture_output=True)
    return secret, public


def release(path, marker='one'):
    path.mkdir()
    binary = path / 'gateway'
    binary.write_text('#!/bin/sh\n# ' + marker + '\nexit 0\n')
    binary.chmod(0o755)
    config = path / 'production.json'
    config.write_bytes(Path('deploy/gateway.example.json').read_bytes())
    (path / 'native-release.json').write_text(json.dumps({
        'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
        'config_sha256': hashlib.sha256(config.read_bytes()).hexdigest()}))
    return path


def test_signed_install_upgrade_and_rollback(tmp_path, signing):
    key, public = signing
    package(release(tmp_path / 'v1'), tmp_path / 'v1.tar.gz', key, '1.0.0')
    root = tmp_path / 'installed'
    assert install(tmp_path / 'v1.tar.gz', root, public) == '1.0.0'
    config_before = (root / 'config/active.json').read_bytes()
    old = (root / 'current').resolve()
    package(release(tmp_path / 'v2', 'two'), tmp_path / 'v2.tar.gz', key, '1.0.1')
    assert install(tmp_path / 'v2.tar.gz', root, public) == '1.0.1'
    assert (root / 'current').resolve() != old
    assert (root / 'config/active.json').read_bytes() == config_before
    assert rollback(root, public) == '1.0.0'
    assert (root / 'current').resolve() == old


def test_tampered_bundle_never_activates(tmp_path, signing):
    key, public = signing
    good = tmp_path / 'good.tar.gz'
    package(release(tmp_path / 'v1'), good, key, '1.0.0')
    bad = tmp_path / 'bad.tar.gz'
    with tarfile.open(good) as source, tarfile.open(bad, 'w:gz') as dest:
        for member in source:
            data = source.extractfile(member).read()
            if member.name == 'gateway': data += b'# tampered'
            member.size = len(data)
            dest.addfile(member, io.BytesIO(data))
    root = tmp_path / 'installed'
    with pytest.raises(ValueError, match='integrity'):
        install(bad, root, public)
    assert not (root / 'current').exists()


def test_traversal_bundle_rejected(tmp_path, signing):
    bad = tmp_path / 'bad.tar.gz'
    with tarfile.open(bad, 'w:gz') as bundle:
        member = tarfile.TarInfo('../escaped')
        member.size = 1
        bundle.addfile(member, io.BytesIO(b'x'))
    with pytest.raises(ValueError, match='unsafe'):
        install(bad, tmp_path / 'installed', signing[1])
    assert not (tmp_path / 'escaped').exists()


def test_wrong_signer_rejected(tmp_path, signing):
    key, public = signing
    bundle = tmp_path / 'signed.tar.gz'
    package(release(tmp_path / 'v1'), bundle, key, '1.0.0')
    other = tmp_path / 'other.pem'
    subprocess.run(['openssl', 'genpkey', '-algorithm', 'ED25519', '-out', str(other)], check=True, capture_output=True)
    subprocess.run(['openssl', 'pkey', '-in', str(other), '-pubout', '-out', str(public)], check=True, capture_output=True)
    with pytest.raises(ValueError, match='signature'):
        install(bundle, tmp_path / 'installed', public)
