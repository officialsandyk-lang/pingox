from __future__ import annotations

import os

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException

from server.agent_adapter import generate_gateway_plan
from server.schemas import PlanRequest, PlanResponse


load_dotenv()

app = FastAPI(
    title="pingora-ai API",
    version="0.1.0-dev-preview",
)


def require_token(authorization: str | None) -> None:
    expected_token = os.environ.get("PINGORA_AI_API_TOKEN", "")

    if not expected_token:
        raise HTTPException(
            status_code=500,
            detail="Server token is not configured",
        )

    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="Missing Authorization header",
        )

    token = authorization.replace("Bearer ", "").strip()

    if token != expected_token:
        raise HTTPException(
            status_code=403,
            detail="Invalid API token",
        )


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "service": "pingora-ai-api",
        "version": "0.1.0-dev-preview",
    }


@app.post("/v1/plan", response_model=PlanResponse)
def plan_gateway(
    request: PlanRequest,
    authorization: str | None = Header(default=None),
) -> PlanResponse:
    require_token(authorization)

    try:
        config = generate_gateway_plan(
            prompt=request.prompt,
            runtime=request.runtime,
            strategy=request.strategy,
            verify=request.verify,
        )

        return PlanResponse(
            ok=True,
            config=config,
            warnings=[],
            explanation="Gateway plan generated successfully.",
        )

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Planning failed: {exc}",
        ) from exc