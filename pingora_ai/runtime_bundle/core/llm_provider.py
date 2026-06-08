from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


SUPPORTED_PROVIDERS = {"ollama", "openai", "hosted", "none"}


class LLMProviderError(RuntimeError):
    """Raised when the selected LLM provider cannot produce a usable plan."""


@dataclass(frozen=True)
class LLMProviderSettings:
    provider: str
    ollama_base_url: str
    ollama_model: str
    openai_model: str
    hosted_base_url: str
    hosted_token: str


def get_llm_settings() -> LLMProviderSettings:
    provider = os.getenv("PINGORA_AI_LLM_PROVIDER", "hosted").strip().lower()

    return LLMProviderSettings(
        provider=provider,
        ollama_base_url=os.getenv("PINGORA_AI_OLLAMA_BASE_URL", "http://127.0.0.1:11434").strip().rstrip("/"),
        ollama_model=os.getenv("PINGORA_AI_OLLAMA_MODEL", "llama3.1:8b").strip(),
        openai_model=os.getenv("PINGORA_AI_OPENAI_MODEL", "gpt-4o-mini").strip(),
        hosted_base_url=os.getenv("PINGORA_AI_API_BASE_URL", "http://127.0.0.1:8000").strip().rstrip("/"),
        hosted_token=os.getenv("PINGORA_AI_API_TOKEN", "").strip(),
    )


def provider_name() -> str:
    return get_llm_settings().provider


def _json_post(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 180,
) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            **(headers or {}),
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise LLMProviderError(f"HTTP {exc.code} from {url}: {body[:2000]}") from exc
    except urllib.error.URLError as exc:
        raise LLMProviderError(f"Could not reach LLM provider at {url}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise LLMProviderError(f"Provider returned invalid JSON from {url}: {exc}") from exc
    except Exception as exc:
        raise LLMProviderError(f"LLM provider request failed for {url}: {exc}") from exc


def _system_prompt() -> str:
    return """
You are an AI Pingora Gateway configuration planner.

Return ONLY valid JSON.
Do not include markdown.
Do not include explanation text.

Your task:
Convert the user's gateway request into a JSON gateway configuration.

Required rules:
- Always include a static "/" route serving "public/index.html" unless user explicitly says no homepage.
- Proxy routes must include path, type, upstream/upstreams, and balancing.
- Valid balancing values:
  single, random, round_robin, weighted_round_robin, ip_hash, least_connections.
- Preserve route-specific algorithm intent.
- Do not let one route's algorithm leak into another route.
- If multiple upstreams are listed and no algorithm is provided, use round_robin.
- If one upstream is listed, use single.
- Preserve weights for weighted_round_robin.
- Do not generate blocked-path routes such as /.env, /.git, /wp-admin, /phpmyadmin.
- Add safe default security fields where appropriate.
- Prefer 127.0.0.1 host addresses in raw plan; runtime conversion can happen later.
"""


def _extract_json_object(text: str) -> dict[str, Any]:
    if not isinstance(text, str) or not text.strip():
        raise LLMProviderError("LLM returned empty content")

    cleaned = text.strip()

    # Remove common fenced-code wrappers.
    cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")

    if start == -1 or end == -1 or end <= start:
        raise LLMProviderError(f"LLM did not return a JSON object. First 500 chars: {cleaned[:500]}")

    candidate = cleaned[start : end + 1]

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise LLMProviderError(f"Could not parse JSON object from LLM output: {exc}") from exc

    if not isinstance(parsed, dict):
        raise LLMProviderError("LLM JSON output must be an object")

    return parsed


def _unwrap_hosted_plan(response: dict[str, Any]) -> dict[str, Any]:
    """
    Your FastAPI planner may return either:
    - raw config
    - {"config": {...}}
    - {"plan": {...}, "config": {...}}
    This normalizes it.
    """
    if not isinstance(response, dict):
        raise LLMProviderError("Hosted API returned non-object response")

    if isinstance(response.get("config"), dict):
        return response["config"]

    if isinstance(response.get("plan"), dict):
        plan = response["plan"]
        if isinstance(plan.get("config"), dict):
            return plan["config"]
        return plan

    return response


def generate_with_ollama(prompt: str) -> dict[str, Any]:
    settings = get_llm_settings()

    payload = {
        "model": settings.ollama_model,
        "stream": False,
        "messages": [
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": prompt},
        ],
        "options": {
            "temperature": 0.1,
        },
    }

    response = _json_post(
        f"{settings.ollama_base_url}/api/chat",
        payload,
        timeout=240,
    )

    content = response.get("message", {}).get("content", "")

    return _extract_json_object(content)


def generate_with_openai(prompt: str) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()

    if not api_key:
        raise LLMProviderError(
            "OPENAI_API_KEY is required when PINGORA_AI_LLM_PROVIDER=openai. "
            "Set it locally in .env or your shell."
        )

    settings = get_llm_settings()

    payload = {
        "model": settings.openai_model,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": prompt},
        ],
    }

    response = _json_post(
        "https://api.openai.com/v1/chat/completions",
        payload,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=240,
    )

    try:
        content = response["choices"][0]["message"]["content"]
    except Exception as exc:
        raise LLMProviderError(f"Unexpected OpenAI response shape: {response}") from exc

    return _extract_json_object(content)


def generate_with_hosted_api(
    prompt: str,
    *,
    runtime: str | None = None,
    strategy: str | None = None,
    verify: str | None = None,
) -> dict[str, Any]:
    settings = get_llm_settings()

    payload = {
        "prompt": prompt,
        "runtime": runtime or os.getenv("PINGORA_AI_RUNTIME", "docker_host"),
        "strategy": strategy or os.getenv("PINGORA_AI_STRATEGY", "bluegreen"),
        "verify": verify or os.getenv("PINGORA_AI_VERIFY", "full"),
    }

    headers: dict[str, str] = {}

    if settings.hosted_token:
        headers["Authorization"] = f"Bearer {settings.hosted_token}"

    response = _json_post(
        f"{settings.hosted_base_url}/v1/plan",
        payload,
        headers=headers,
        timeout=240,
    )

    return _unwrap_hosted_plan(response)


def generate_without_llm(prompt: str) -> dict[str, Any]:
    raise LLMProviderError(
        "PINGORA_AI_LLM_PROVIDER=none is selected. "
        "Natural-language planning is disabled. "
        "Use --config-file or structured commands such as add/edit/remove/verify."
    )


def generate_gateway_plan(
    prompt: str,
    *,
    runtime: str | None = None,
    strategy: str | None = None,
    verify: str | None = None,
) -> dict[str, Any]:
    """
    Main provider router.

    LLM provider only creates a proposed plan/config.
    The caller must still run deterministic repair, validation, security defaults,
    mutation decision gates, deployment, and verification.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise LLMProviderError("Prompt is required")

    settings = get_llm_settings()
    provider = settings.provider

    if provider not in SUPPORTED_PROVIDERS:
        raise LLMProviderError(
            f"Unsupported PINGORA_AI_LLM_PROVIDER={provider!r}. "
            "Use one of: ollama, openai, hosted, none."
        )

    if provider == "ollama":
        return generate_with_ollama(prompt)

    if provider == "openai":
        return generate_with_openai(prompt)

    if provider == "hosted":
        return generate_with_hosted_api(
            prompt,
            runtime=runtime,
            strategy=strategy,
            verify=verify,
        )

    if provider == "none":
        return generate_without_llm(prompt)

    raise LLMProviderError(f"Unsupported provider: {provider}")


def check_provider_health() -> dict[str, Any]:
    """
    Lightweight health check for doctor/setup commands.
    Does not generate a gateway plan.
    """
    settings = get_llm_settings()
    provider = settings.provider

    result: dict[str, Any] = {
        "provider": provider,
        "ok": False,
        "details": {},
    }

    if provider == "none":
        result["ok"] = True
        result["details"] = {
            "message": "No-LLM mode selected. Natural-language planning disabled.",
        }
        return result

    if provider == "ollama":
        try:
            url = f"{settings.ollama_base_url}/api/tags"
            request = urllib.request.Request(url, method="GET")

            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read().decode("utf-8", errors="replace")
                tags = json.loads(raw)

            models = [
                item.get("name")
                for item in tags.get("models", [])
                if isinstance(item, dict)
            ]

            result["ok"] = settings.ollama_model in models
            result["details"] = {
                "base_url": settings.ollama_base_url,
                "model": settings.ollama_model,
                "available_models": models,
                "message": "Ollama reachable and selected model found"
                if result["ok"]
                else "Ollama reachable but selected model not found",
            }
            return result
        except Exception as exc:
            result["details"] = {
                "base_url": settings.ollama_base_url,
                "model": settings.ollama_model,
                "error": str(exc),
            }
            return result

    if provider == "openai":
        api_key_present = bool(os.getenv("OPENAI_API_KEY", "").strip())
        result["ok"] = api_key_present
        result["details"] = {
            "model": settings.openai_model,
            "api_key_present": api_key_present,
            "message": "OPENAI_API_KEY present" if api_key_present else "OPENAI_API_KEY missing",
        }
        return result

    if provider == "hosted":
        try:
            url = f"{settings.hosted_base_url}/health"
            request = urllib.request.Request(url, method="GET")
            if settings.hosted_token:
                request.add_header("Authorization", f"Bearer {settings.hosted_token}")

            with urllib.request.urlopen(request, timeout=15) as response:
                body = response.read().decode("utf-8", errors="replace")

            result["ok"] = True
            result["details"] = {
                "base_url": settings.hosted_base_url,
                "health": body[:500],
            }
            return result
        except Exception as exc:
            result["details"] = {
                "base_url": settings.hosted_base_url,
                "error": str(exc),
            }
            return result

    result["details"] = {
        "error": f"Unsupported provider: {provider}",
    }
    return result
