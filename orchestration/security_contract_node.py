from __future__ import annotations

from typing import Any

from core.security_policy_contract import (
    apply_security_policy_contract,
    verify_security_policy_contract,
)


def run_security_contract_node(state: dict[str, Any]) -> dict[str, Any]:
    config = state.get("config") or state.get("gateway_config") or {}

    if not isinstance(config, dict):
        state["security_contract_result"] = {
            "ok": False,
            "failed_checks": [{"name": "config must be dict", "ok": False}],
        }
        state["error"] = "Security contract failed: config must be dictionary"
        state["next_action"] = "repair_config"
        return state

    runtime = (
        state.get("runtime")
        or state.get("runtime_mode")
        or config.get("runtime")
        or config.get("runtime_mode")
        or "local"
    )

    secured = apply_security_policy_contract(config, environment=str(runtime))
    result = verify_security_policy_contract(secured, strict=False)

    state["config"] = secured
    state["gateway_config"] = secured
    state["security_contract_result"] = result

    if not result.get("ok"):
        failed = result.get("failed_checks", [])
        reason = "; ".join(item.get("name", "unknown") for item in failed)

        state["error"] = "Security contract rejected config: " + reason
        state["next_action"] = "repair_config"
        state["security_contract_failed"] = True
        return state

    state["security_contract_failed"] = False
    state["next_action"] = "continue"
    return state
