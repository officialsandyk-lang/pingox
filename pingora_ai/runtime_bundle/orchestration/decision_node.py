from __future__ import annotations

from typing import Any

from core.decision_engine import (
    apply_deterministic_repairs,
    evaluate_config,
    normalize_runtime,
    normalize_strategy,
)


def run_decision_node(state: dict[str, Any]) -> dict[str, Any]:
    """
    Deterministic decision gate for LangGraph.

    This node makes config/routing/runtime decisions without LLM judgement.
    It repairs safe deterministic issues, then rejects unsafe config drift.
    """

    config = state.get("config") or state.get("gateway_config") or {}

    if not isinstance(config, dict):
        state["decision_result"] = {
            "ok": False,
            "success": False,
            "failed_decisions": [
                {
                    "category": "config_shape",
                    "reason": "Config must be a dictionary.",
                    "required_action": "regenerate_config",
                }
            ],
        }
        state["error"] = "Decision engine rejected config: config must be a dictionary"
        state["next_action"] = "repair_config"
        return state

    runtime = normalize_runtime(
        state.get("runtime")
        or state.get("runtime_mode")
        or config.get("runtime")
        or config.get("runtime_mode")
        or "local"
    )

    strategy = normalize_strategy(
        state.get("strategy")
        or config.get("strategy")
        or config.get("deployment_strategy"),
        runtime,
    )

    config["runtime"] = runtime
    config["runtime_mode"] = runtime
    config["strategy"] = strategy
    config["deployment_strategy"] = strategy

    repaired = apply_deterministic_repairs(config, runtime=runtime)
    decision_result = evaluate_config(repaired)

    state["config"] = repaired
    state["gateway_config"] = repaired
    state["decision_result"] = decision_result
    state["runtime"] = runtime
    state["runtime_mode"] = runtime
    state["strategy"] = strategy

    if not decision_result.get("ok"):
        failed = decision_result.get("failed_decisions", [])
        reason = "; ".join(item.get("reason", "unknown") for item in failed)

        state["error"] = "Decision engine rejected config: " + reason
        state["next_action"] = "repair_config"
        state["decision_failed"] = True
        return state

    state["decision_failed"] = False
    state["next_action"] = "continue"
    return state
