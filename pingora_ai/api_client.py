from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


DEFAULT_API_URL = "http://127.0.0.1:8000"


def get_api_url() -> str:
    return (
        os.environ.get("PINGORA_AI_API_BASE_URL")
        or os.environ.get("PINGORA_AI_API_URL")
        or DEFAULT_API_URL
    ).strip().rstrip("/")


def get_api_token() -> str:
    return os.environ.get("PINGORA_AI_API_TOKEN", "dev-token-change-this").strip()


def post_json(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    api_url = get_api_url()
    token = get_api_token()

    request = urllib.request.Request(
        url=f"{api_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "pingora-ai-cli/0.1.0-dev-preview",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))

    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API error HTTP {exc.code}: {body}") from exc

    except Exception as exc:
        raise RuntimeError(f"API request failed: {exc}") from exc


def plan_gateway(
    prompt: str,
    runtime: str = "docker_host",
    strategy: str = "bluegreen",
    verify: str = "full",
) -> dict[str, Any]:
    return post_json(
        "/v1/plan",
        {
            "prompt": prompt,
            "runtime": runtime,
            "strategy": strategy,
            "verify": verify,
        },
    )