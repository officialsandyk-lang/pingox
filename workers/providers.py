"""HTTP protocol adapters for operator-selected AI endpoints.

Endpoints are configured by the operator, never taken from an end-user prompt.
No redirect following, automatic provider fallback, or credential reuse.
"""
from __future__ import annotations
import ipaddress
import json
import os
from urllib.parse import urlsplit
import httpx


class ProviderError(RuntimeError):
    pass


def _endpoint(base: str, path: str) -> str:
    parsed = urlsplit(base)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
        raise ProviderError("AI_BASE_URL must be an endpoint without credentials, query or fragment")
    local = parsed.hostname == "localhost"
    try:
        local = local or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        pass
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise ProviderError("AI endpoints require HTTPS except on loopback")
    return base.rstrip("/") + path


def _secret(variable: str) -> str:
    file = os.getenv(variable + "_FILE")
    if file and os.getenv(variable):
        raise ProviderError(f"Set only {variable} or {variable}_FILE")
    if file:
        from pathlib import Path
        value = Path(file).read_text().strip()
    else:
        value = os.getenv(variable, "").strip()
    if not value:
        raise ProviderError(f"Missing {variable} or {variable}_FILE")
    return value


def request_text(backend: str, instructions: str, content: str, *, model: str, timeout: float) -> str:
    messages = [{"role": "system", "content": instructions}, {"role": "user", "content": content}]
    headers = {}
    if backend == "openai-compatible":
        base = os.getenv("AI_BASE_URL")
        if not base:
            raise ProviderError("openai-compatible requires AI_BASE_URL")
        url = _endpoint(base, "/chat/completions")
        headers = {"Authorization": "Bearer " + _secret("AI_API_KEY")}
        payload = {"model": model, "messages": messages, "max_tokens": 12000, "stream": False}
    elif backend == "anthropic":
        url = _endpoint(os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1"), "/messages")
        headers = {"x-api-key": _secret("ANTHROPIC_API_KEY"),
                   "anthropic-version": os.getenv("ANTHROPIC_API_VERSION", "2023-06-01")}
        payload = {"model": model, "system": instructions, "messages": messages[1:], "max_tokens": 12000}
    elif backend == "ollama":
        url = _endpoint(os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434"), "/api/chat")
        payload = {"model": model, "messages": messages, "stream": False, "options": {"num_predict": 12000}}
        if os.getenv("OLLAMA_API_KEY") or os.getenv("OLLAMA_API_KEY_FILE"):
            headers = {"Authorization": "Bearer " + _secret("OLLAMA_API_KEY")}
    else:
        raise ProviderError("Unsupported AI protocol")
    try:
        # Stream the HTTP bytes only, with an explicit response size cap. Model
        # streaming is disabled so completion/refusal state can be validated.
        with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            with client.stream("POST", url, json=payload, headers=headers) as response:
                response.raise_for_status()
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 512_000:
                        raise ProviderError("AI response exceeds 512 KB")
        result = json.loads(raw)
        if backend == "openai-compatible":
            choice = result["choices"][0]
            message = choice["message"]
            if choice.get("finish_reason") != "stop" or message.get("refusal") or message.get("tool_calls"):
                raise ProviderError("AI response was incomplete, refused or requested tools")
            text = message["content"]
        elif backend == "anthropic":
            if result.get("stop_reason") != "end_turn":
                raise ProviderError("AI response did not finish normally")
            blocks = result["content"]
            if any(block.get("type") not in {"text", "thinking", "redacted_thinking"} for block in blocks):
                raise ProviderError("AI returned unsupported content")
            text = "".join(block["text"] for block in blocks if block.get("type") == "text")
        else:
            if result.get("done") is not True or result.get("done_reason") != "stop":
                raise ProviderError("AI response did not finish normally")
            if result["message"].get("tool_calls"):
                raise ProviderError("AI requested tools")
            text = result["message"]["content"]
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("AI returned empty or non-text output")
        return text.strip()
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError(f"AI provider request failed ({type(exc).__name__})") from None
