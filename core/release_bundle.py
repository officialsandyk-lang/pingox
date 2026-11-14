"""Signed native bundles with verified extraction and reversible activation."""
from __future__ import annotations
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
from core.config_store import atomic_write, locked, publish, read_config
from core.local_service import digest

FILES = {'gateway', 'runtime/active.json', 'LICENSE', 'THIRD_PARTY_NOTICES.txt', 'Cargo.lock'}
METADATA = {'manifest.json', 'manifest.sig'}
MAX_FILE = 256 * 1024 * 1024


def openssl(*args: str) -> None:
    result = subprocess.run(['openssl', *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode:
        raise ValueError('release signature operation failed')


def package(release: Path, output: Path, key: Path, version: str) -> None:
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?', version):
        raise ValueError('version must be a semantic version without path characters')
    if output.exists():
        raise ValueError('bundle output already exists')
    release = release.resolve()
    # Native build records guard against accidentally bundling edited artifacts.
    native = json.loads((release / 'native-release.json').read_text())
    if native.get('binary_sha256') != digest(release / 'gateway') or native.get('config_sha256') != digest(release / 'production.json'):
        raise ValueError('native release changed after build')
    config = read_config(release / 'production.json')
    data = {'gateway': (release / 'gateway').read_bytes(),
            'runtime/active.json': (config.model_dump_json(indent=2) + '\n').encode()}
    for name in ('LICENSE', 'THIRD_PARTY_NOTICES.txt', 'Cargo.lock'):
        data[name] = (release / name).read_bytes()
    if any(len(value) > (MAX_FILE if name == 'gateway' else 4 * 1024 * 1024) for name, value in data.items()):
        raise ValueError('release file too large')
    manifest = {'format': 1, 'version': version,
                'files': {name: hashlib.sha256(value).hexdigest() for name, value in data.items()}}
    data['manifest.json'] = (json.dumps(manifest, sort_keys=True, separators=(',', ':')) + '\n').encode()
    with tempfile.TemporaryDirectory(prefix='pingox-sign-') as temporary:
        directory = Path(temporary)
        (directory / 'manifest.json').write_bytes(data['manifest.json'])
        openssl('pkeyutl', '-sign', '-rawin', '-inkey', str(key.resolve()), '-in', str(directory / 'manifest.json'), '-out', str(directory / 'manifest.sig'))
        data['manifest.sig'] = (directory / 'manifest.sig').read_bytes()
        # Exclusive creation protects a previously published artifact.
        with output.open('xb') as stream:
            with tarfile.open(fileobj=stream, mode='w:gz') as bundle:
                for name, value in data.items():
                    member = tarfile.TarInfo(name)
                    member.size = len(value)
                    member.mode = 0o755 if name == 'gateway' else 0o644
                    member.mtime = 0
                    bundle.addfile(member, io.BytesIO(value))


def verify(directory: Path, public_key: Path) -> dict:
    openssl('pkeyutl', '-verify', '-rawin', '-pubin', '-inkey', str(public_key.resolve()),
            '-in', str(directory / 'manifest.json'), '-sigfile', str(directory / 'manifest.sig'))
    manifest = json.loads((directory / 'manifest.json').read_text())
    if manifest.get('format') != 1 or set(manifest.get('files', {})) != FILES:
        raise ValueError('unsupported release manifest')
    for name, expected in manifest['files'].items():
        file = directory / name
        if file.is_symlink() or not file.is_file() or digest(file) != expected:
            raise ValueError('release file integrity check failed')
    read_config(directory / 'runtime/active.json')
    return manifest


def extract(bundle: Path, directory: Path) -> None:
    seen = set()
    with tarfile.open(bundle, 'r:gz') as archive:
        for member in archive:
            if member.name not in FILES | METADATA or member.name in seen or not member.isfile():
                raise ValueError('unexpected or unsafe bundle member')
            limit = MAX_FILE if member.name == 'gateway' else 4 * 1024 * 1024
            if not 0 < member.size <= limit:
                raise ValueError('bundle member exceeds size limit')
            seen.add(member.name)
            target = directory / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, target.open('xb') as destination:
                shutil.copyfileobj(source, destination)
            target.chmod(0o755 if member.name == 'gateway' else 0o644)
    if seen != FILES | METADATA:
        raise ValueError('incomplete bundle')


def pointer(root: Path, name: str, target: Path) -> None:
    temporary = root / ('.' + name + '-pending')
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    temporary.symlink_to(target.relative_to(root))
    os.replace(temporary, root / name)
    fd = os.open(root, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def current_release(root: Path, name: str) -> Path | None:
    link = root / name
    if not link.is_symlink():
        if link.exists(): raise ValueError(f'{name} must be a managed release symlink')
        return None
    target = link.resolve(strict=True)
    if target.parent != (root / 'releases').resolve():
        raise ValueError('release pointer escapes managed directory')
    return target


def install(bundle: Path, root: Path, public_key: Path) -> str:
    root = root.resolve()
    with locked(root):
        releases = root / 'releases'
        releases.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.stage-', dir=releases) as temporary:
            stage = Path(temporary)
            extract(bundle, stage)
            manifest = verify(stage, public_key)
            identity = digest(stage / 'manifest.json')
            destination = releases / identity
            # Execute only after verifying against the operator's trusted key.
            config_path = root / 'config/active.json'
            check = config_path if config_path.exists() else stage / 'runtime/active.json'
            result = subprocess.run([str(stage / 'gateway'), '--check-config', str(check)], capture_output=True)
            if result.returncode:
                raise ValueError('new gateway rejected configuration; active release unchanged')
            previous = current_release(root, 'current')
            if destination.exists():
                verify(destination, public_key)
            else:
                shutil.copytree(stage, destination)
            if not config_path.exists():
                publish(root / 'config', read_config(stage / 'runtime/active.json'), 0)
            write_service_unit(root)
            if previous and previous != destination:
                pointer(root, 'previous', previous)
            pointer(root, 'current', destination)
            return manifest['version']


def rollback(root: Path, public_key: Path) -> str:
    root = root.resolve()
    with locked(root):
        previous = current_release(root, 'previous')
        current = current_release(root, 'current')
        if not previous or not current:
            raise ValueError('no previous release to activate')
        manifest = verify(previous, public_key)
        result = subprocess.run([str(previous / 'gateway'), '--check-config', str(root / 'config/active.json')], capture_output=True)
        if result.returncode:
            raise ValueError('previous gateway cannot read current configuration')
        pointer(root, 'current', previous)
        pointer(root, 'previous', current)
        return manifest['version']


def write_service_unit(root: Path) -> None:
    # Reject systemd specifier/argument injection in an operator-selected install path.
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', str(root)):
        return  # Installation remains usable with a custom supervisor.
    config = read_config(root / 'config/active.json')
    credentials = (f'LoadCredential=api_keys:{root}/secrets/api-keys.json\nEnvironment=PINGOX_API_KEYS_FILE=%d/api_keys\n'
                   if config.security.require_api_key else '')
    unit = f"""[Unit]
Description=Pingox Gateway
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
DynamicUser=yes
WorkingDirectory={root}/current
ExecStart={root}/current/gateway
Environment=GATEWAY_BIND_HOST=127.0.0.1
Environment=PINGOX_CONFIG_FILE={root}/config/active.json
{credentials}Restart=on-failure
RestartSec=3
TimeoutStopSec=45
KillSignal=SIGTERM
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
MemoryMax=512M
TasksMax=256
LimitNOFILE=65536
[Install]
WantedBy=multi-user.target
"""
    # A generated unit is reviewable; this command never changes the OS service manager.
    atomic_write(root / 'pingox.service', unit.encode())
