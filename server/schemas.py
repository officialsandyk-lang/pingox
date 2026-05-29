from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class PlanRequest(BaseModel):
    prompt: str = Field(..., min_length=5, max_length=8000)
    runtime: str = "docker_host"
    strategy: str = "bluegreen"
    verify: str = "full"


class PlanResponse(BaseModel):
    ok: bool
    config: dict[str, Any]
    warnings: list[str] = []
    explanation: str = ""