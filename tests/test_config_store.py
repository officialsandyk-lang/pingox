from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import json
import pytest
from pydantic import ValidationError
from core.config_store import publish, read_config
from core.production import ProductionConfig


def sample():
    return ProductionConfig.model_validate_json(Path('deploy/gateway.example.json').read_text())


def test_rollback_is_a_new_revision(tmp_path):
    first = publish(tmp_path, sample(), 0)
    second = first.model_copy(deep=True)
    second.routes[0].path = '/new'
    assert publish(tmp_path, second, 1).revision == 2
    restored = publish(tmp_path, None, 2)
    assert restored.revision == 3 and restored.routes[0].path == '/api'
    events = [json.loads(line) for line in (tmp_path / 'audit.jsonl').read_text().splitlines()]
    assert [e['action'] for e in events if e['state'] == 'published'] == ['apply', 'apply', 'rollback']


def test_competing_writers_do_not_overwrite(tmp_path):
    publish(tmp_path, sample(), 0)
    def writer(_):
        try:
            publish(tmp_path, sample(), 1)
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=8) as executor:
        assert sum(executor.map(writer, range(8))) == 1
    assert read_config(tmp_path / 'active.json').revision == 2


def test_invalid_candidate_leaves_current_unchanged(tmp_path):
    publish(tmp_path, sample(), 0)
    before = (tmp_path / 'active.json').read_bytes()
    bad = sample().model_copy(update={'port': 0})
    with pytest.raises(ValidationError):
        publish(tmp_path, bad, 1)
    assert (tmp_path / 'active.json').read_bytes() == before


def test_history_is_bounded(tmp_path):
    for revision in range(15):
        publish(tmp_path, sample(), revision)
    assert len(list((tmp_path / 'history').glob('*.json'))) == 10
    assert read_config(tmp_path / 'active.json').revision == 15


def test_rollback_without_history_fails(tmp_path):
    with pytest.raises(ValueError, match='no previous'):
        publish(tmp_path, None, 0)


@pytest.mark.parametrize('upstream', [
    {'address': 'example.com:443', 'tls': True},
    {'address': 'example.com:80', 'server_name': 'example.com'},
    {'address': 'example.com:443', 'tls': True, 'server_name': 'bad/name'},
])
def test_tls_policy_fails_closed(upstream):
    data = sample().model_dump()
    data['routes'][0]['upstreams'] = [upstream]
    with pytest.raises(ValidationError):
        ProductionConfig.model_validate(data)
