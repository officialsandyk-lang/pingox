from __future__ import annotations

import json
import os
from typing import Any


def _get_openai_client():
    """
    Lazy OpenAI client.

    Hosted/user runtime mode must not require OPENAI_API_KEY.
    Only direct OpenAI planner mode should instantiate OpenAI().
    """
    api_key = os.getenv("OPENAI_API_KEY", "").strip()

    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. This is only required for direct OpenAI planner mode. "
            "Hosted mode should call the FastAPI planner before runtime deploy."
        )

    from openai import OpenAI

    return OpenAI(api_key=api_key)


def prompt_to_config(prompt: str) -> dict[str, Any]:
    """
    Fallback local OpenAI prompt planner.

    In hosted CLI mode this should normally not be used because the hosted
    FastAPI planner already returns the config.
    """
    client = _get_openai_client()

    response = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        messages=[
            {
                "role": "system",
                "content": (
                    "Return only valid JSON for an AI Pingora gateway config. "
                    "Include port, routes, runtime, strategy, verify, and security."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0,
    )

    content = response.choices[0].message.content or "{}"
    content = content.strip()

    if content.startswith("```"):
        content = content.strip("`")
        content = content.replace("json\n", "", 1).replace("json", "", 1).strip()

    return json.loads(content)
