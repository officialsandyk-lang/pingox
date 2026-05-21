from __future__ import annotations

from typing import Any

try:
    from langsmith import traceable
except Exception:
    def traceable(*args, **kwargs):
        def decorator(fn):
            return fn
        return decorator


@traceable(name="node_security_agent", run_type="chain")
def security_node(state: dict[str, Any]) -> dict[str, Any]:
    from agents.security_agent import enforce_security

    state = dict(state)
    config = state.get("config") or {}
    prompt = state.get("prompt") or ""

    state["config"] = enforce_security(config, prompt=prompt)
    state["security_agent_result"] = {"success": True}

    return state


@traceable(name="node_config_verification_agent", run_type="chain")
def config_verification_node(state: dict[str, Any]) -> dict[str, Any]:
    from agents.config_verification_agent import run_config_verification_agent

    state = dict(state)
    config = state.get("config") or {}

    runtime = (
        state.get("runtime_mode")
        or state.get("runtime")
        or config.get("runtime_mode")
        or config.get("runtime")
        or "local"
    )

    strategy = (
        state.get("strategy")
        or state.get("deployment_strategy")
        or config.get("strategy")
        or config.get("deployment_strategy")
        or "direct"
    )

    level = state.get("verify_level") or state.get("level") or "full"
    live_url = state.get("live_url") or f"http://127.0.0.1:{int(config.get('port') or 8090)}/"

    result = run_config_verification_agent(
        config=config,
        live_url=live_url,
        runtime=runtime,
        strategy=strategy,
        level=level,
        environment=runtime,
    )

    if not result.get("success"):
        raise RuntimeError(
            "Config verification agent blocked deployment:\n"
            + "\n".join(str(x) for x in result.get("failures") or [])
        )

    state["config"] = result.get("config") or config
    state["config_verification_agent_result"] = result

    return state


@traceable(name="node_runtime_agent", run_type="chain")
def runtime_check_node(state: dict[str, Any]) -> dict[str, Any]:
    from agents.runtime_agent import run_gateway_runtime

    state = dict(state)
    config = state.get("config") or {}

    runtime = (
        state.get("runtime_mode")
        or state.get("runtime")
        or config.get("runtime_mode")
        or config.get("runtime")
        or "local"
    )

    project_dir = state.get("project_dir") or "generated-pingora-proxy"
    port = int(config.get("port") or state.get("port") or 8090)
    live_url = state.get("live_url")

    result = run_gateway_runtime(
        runtime=runtime,
        project_dir=project_dir,
        port=port,
        health_url=live_url,
    )

    state["runtime_agent_result"] = result

    if not result.get("success"):
        raise RuntimeError(f"Runtime agent failed: {result}")

    return state


@traceable(name="node_root_cause_agent", run_type="chain")
def root_cause_node(state: dict[str, Any]) -> dict[str, Any]:
    from agents.root_cause_agent import diagnose_and_repair_config

    state = dict(state)
    config = state.get("config") or {}
    evidence = state.get("failure_evidence") or state.get("verification") or state

    result = diagnose_and_repair_config(
        config,
        evidence=evidence,
        auto_repair=True,
        store=True,
    )

    state["root_cause_agent_result"] = result

    if result.get("changed"):
        state["config"] = result.get("repaired_config") or config

    return state
