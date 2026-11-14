"""Local, revision-checked configuration publication. No shell or remote access."""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from contextlib import contextmanager
from core.production import ProductionConfig

LIMIT = 4 * 1024 * 1024


def read_config(path: Path) -> ProductionConfig:
    with path.open('rb') as stream:
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError('configuration exceeds 4 MiB')
    return ProductionConfig.model_validate_json(data)


def atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    fd, temporary = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def locked(directory: Path):
    directory.mkdir(parents=True, exist_ok=True, mode=0o755)
    with (directory / '.lock').open('a') as lock:
        os.chmod(directory / '.lock', 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def publish(directory: Path, candidate: ProductionConfig | None, expected: int) -> ProductionConfig:
    directory = directory.resolve()
    with locked(directory):
        active = directory / 'active.json'
        current = read_config(active) if active.exists() else None
        revision = current.revision if current else 0
        if expected != revision:
            raise ValueError(f'configuration changed: expected revision {expected}, found {revision}')
        history = directory / 'history'
        history.mkdir(exist_ok=True, mode=0o700)
        action = 'apply'
        if candidate is None:
            action = 'rollback'
            previous = sorted((int(p.stem), p) for p in history.glob('*.json')
                              if p.stem.isdigit() and int(p.stem) < revision)
            if not previous:
                raise ValueError('no previous configuration is available')
            candidate = read_config(previous[-1][1])
        # Validation includes the new revision bound, even when the caller used model_copy.
        value = candidate.model_dump()
        value['revision'] = revision + 1
        candidate = ProductionConfig.model_validate(value)
        data = (candidate.model_dump_json(indent=2) + '\n').encode()
        if len(data) > LIMIT:
            raise ValueError('configuration exceeds 4 MiB')
        if current:
            atomic_write(history / f'{revision}.json', (current.model_dump_json(indent=2) + '\n').encode(), 0o600)
        # Crash-safe publication: active.json is always the complete old or new revision.
        # Audit is a local operational record, not a tamper-proof compliance ledger.
        event = {'time_unix': int(time.time()), 'action': action, 'revision': candidate.revision,
                 'sha256': hashlib.sha256(data).hexdigest(), 'uid': os.getuid()}
        with (directory / 'audit.jsonl').open('a') as log:
            os.chmod(directory / 'audit.jsonl', 0o600)
            log.write(json.dumps({**event, 'state': 'prepared'}) + '\n')
            log.flush()
            os.fsync(log.fileno())
            atomic_write(active, data)
            log.write(json.dumps({**event, 'state': 'published'}) + '\n')
            log.flush()
            os.fsync(log.fileno())
        # Keep a bounded rollback history. Audit rotation is an operator responsibility.
        versions = sorted((int(p.stem), p) for p in history.glob('*.json') if p.stem.isdigit())
        for _, path in versions[:-10]:
            path.unlink()
        return candidate
