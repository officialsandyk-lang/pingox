import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from workers.ai_worker import WorkerError, run_worker, json_object


def test_disabled_worker_never_calls_network(monkeypatch):
    monkeypatch.setenv("AI_WORKER_BACKEND", "disabled")
    with pytest.raises(WorkerError, match="disabled"):
        run_worker("instructions", "input")


def test_json_rejects_arrays_and_invalid_output():
    for value in ('[]', '```json\n{}\n```', ''):
        with pytest.raises(WorkerError):
            json_object(value)


def test_codex_is_bounded_and_does_not_receive_application_secrets(monkeypatch):
    monkeypatch.setenv("AI_WORKER_BACKEND", "codex")
    monkeypatch.setenv("DATABASE_PASSWORD", "must-not-pass")
    def run(command, **kwargs):
        assert "--sandbox" in command and "read-only" in command
        assert "--ignore-user-config" in command
        assert "DATABASE_PASSWORD" not in kwargs["env"]
        assert kwargs["timeout"] <= 600
        assert kwargs["input"].endswith("INPUT:\nprompt")
        output = Path(command[command.index("--output-last-message") + 1])
        output.write_text(json.dumps({"result": "{}"}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr("workers.ai_worker.subprocess.run", run)
    assert run_worker("instructions", "prompt") == "{}"


def test_responses_has_no_retries_and_rejects_truncation(monkeypatch):
    monkeypatch.setenv("AI_WORKER_BACKEND", "responses")
    class Client:
        def __init__(self, **kwargs):
            assert kwargs["max_retries"] == 0
            self.responses = self
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def create(self, **kwargs):
            assert kwargs["store"] is False
            return SimpleNamespace(status="incomplete", output_text='{"port":8080}')
    monkeypatch.setattr("openai.OpenAI", Client)
    with pytest.raises(WorkerError, match="incomplete"):
        run_worker("instructions", "prompt")


def test_imports_do_not_require_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import importlib
    import ai.ai_config
    import agents.debug_agent
    importlib.reload(ai.ai_config)
    importlib.reload(agents.debug_agent)
