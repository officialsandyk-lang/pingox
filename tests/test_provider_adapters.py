import json
import httpx
import pytest
from workers.providers import request_text, ProviderError


@pytest.mark.parametrize('backend,payload,expected_path', [
    ('openai-compatible', {'choices': [{'finish_reason': 'stop', 'message': {'content': '{}'}}]}, '/v1/chat/completions'),
    ('anthropic', {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': '{}'}]}, '/v1/messages'),
    ('ollama', {'done': True, 'done_reason': 'stop', 'message': {'content': '{}'}}, '/api/chat'),
])
def test_protocol_contracts(monkeypatch, backend, payload, expected_path):
    monkeypatch.setenv('AI_BASE_URL', 'https://models.example/v1')
    monkeypatch.setenv('AI_API_KEY', 'test-only')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-only')
    def respond(request):
        assert request.url.path == expected_path
        body = json.loads(request.content)
        assert body['model'] == 'test-model'
        assert 'tools' not in body
        if backend != 'anthropic': assert body['stream'] is False
        if backend == 'ollama': assert 'authorization' not in request.headers
        return httpx.Response(200, json=payload)
    client = httpx.Client
    monkeypatch.setattr('workers.providers.httpx.Client', lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    assert request_text(backend, 'rules', 'input', model='test-model', timeout=5) == '{}'


@pytest.mark.parametrize('url', ['http://remote.example/v1', 'https://user:secret@remote.example/v1', 'https://models.example/v1?key=x'])
def test_insecure_or_embedded_credentials_rejected(monkeypatch, url):
    monkeypatch.setenv('AI_BASE_URL', url)
    with pytest.raises(ProviderError):
        request_text('openai-compatible', 'rules', 'input', model='x', timeout=5)


@pytest.mark.parametrize('payload', [
    {'choices': [{'finish_reason': 'length', 'message': {'content': '{}'}}]},
    {'choices': [{'finish_reason': 'stop', 'message': {'content': '{}', 'refusal': 'no'}}]},
    {'choices': [{'finish_reason': 'stop', 'message': {'content': '', 'tool_calls': [{}]}}]},
    {'error': {'message': 'private-key-must-not-leak'}},
])
def test_failure_and_truncation_are_not_treated_as_valid_output(monkeypatch, payload):
    monkeypatch.setenv('AI_BASE_URL', 'https://models.example/v1')
    monkeypatch.setenv('AI_API_KEY', 'test-only')
    client = httpx.Client
    monkeypatch.setattr('workers.providers.httpx.Client', lambda **kwargs: client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)), **kwargs))
    with pytest.raises(ProviderError) as error:
        request_text('openai-compatible', 'rules', 'input', model='x', timeout=5)
    assert 'private-key' not in str(error.value)


def test_secret_file_conflict_is_rejected(monkeypatch, tmp_path):
    from workers.providers import _secret
    secret = tmp_path / 'key'
    secret.write_text('file-key')
    monkeypatch.setenv('AI_API_KEY_FILE', str(secret))
    monkeypatch.delenv('AI_API_KEY', raising=False)
    assert _secret('AI_API_KEY') == 'file-key'
    monkeypatch.setenv('AI_API_KEY', 'env-key')
    with pytest.raises(ProviderError, match='only'):
        _secret('AI_API_KEY')


@pytest.mark.parametrize('status,body,headers', [
    (302, b'', {'location': 'https://another-provider.example/steal'}),
    (401, b'private-api-key-do-not-print', {}),
    (200, b'x' * 512001, {}),
])
def test_redirect_auth_and_oversized_failures_are_safe(monkeypatch, status, body, headers):
    monkeypatch.setenv('AI_BASE_URL', 'https://models.example/v1')
    monkeypatch.setenv('AI_API_KEY', 'test-only')
    calls = []
    def respond(request):
        calls.append(request.url.host)
        return httpx.Response(status, content=body, headers=headers)
    client = httpx.Client
    monkeypatch.setattr('workers.providers.httpx.Client', lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    with pytest.raises(ProviderError) as error:
        request_text('openai-compatible', 'rules', 'input', model='x', timeout=5)
    assert calls == ['models.example']
    assert 'private-api-key' not in str(error.value)
