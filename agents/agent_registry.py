from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class AgentSpec:
    name: str
    module: str
    entrypoint: str
    role: str
    tier: str
    production_path: bool


CORE_AGENTS = [
    AgentSpec("config_update_agent", "agents.config_update_agent", "apply_config_update", "Parse update intent and mutate config.", "core", True),
    AgentSpec("security_agent", "agents.security_agent", "enforce_security", "Apply baseline gateway security policy.", "core", True),
    AgentSpec("config_verification_agent", "agents.config_verification_agent", "run_config_verification_agent", "Validate config, routes, algorithms, upstreams, weights and production contract.", "core", True),
    AgentSpec("runtime_agent", "agents.runtime_agent", "run_gateway_runtime", "Check local/Docker/Kubernetes runtime readiness.", "core", True),
    AgentSpec("deployment_repair_agent", "agents.deployment_repair_agent", "run_docker_command_with_repair", "Repair Docker/build/deployment failures.", "core", True),
    AgentSpec("root_cause_agent", "agents.root_cause_agent", "diagnose_and_repair_config", "Classify failures, repair known config failures and store MTTR learning.", "core", True),
]

HELPER_AGENTS = [
    AgentSpec("control_plane_repair_agent", "agents.control_plane_repair_agent", "resolve_update_flow", "Compatibility helper for old update graph entrypoints.", "helper", False),
    AgentSpec("debug_agent", "agents.debug_agent", "fix_rust_code", "AI Rust code repair fallback.", "helper", False),
    AgentSpec("reliability_agent", "agents.reliability_agent", "run_reliability_agent", "Readiness summary/reporting helper.", "helper", False),
]

ALL_AGENTS = CORE_AGENTS + HELPER_AGENTS


def load_agent(spec: AgentSpec) -> Callable[..., Any]:
    module = importlib.import_module(spec.module)
    fn = getattr(module, spec.entrypoint, None)

    if not callable(fn):
        raise RuntimeError(
            f"Agent {spec.name} missing callable entrypoint "
            f"{spec.module}.{spec.entrypoint}"
        )

    return fn


def validate_agent_registry() -> dict[str, Any]:
    results = []

    for spec in ALL_AGENTS:
        try:
            load_agent(spec)
            ok = True
            error = None
        except Exception as exc:
            ok = False
            error = str(exc)

        results.append(
            {
                "name": spec.name,
                "module": spec.module,
                "entrypoint": spec.entrypoint,
                "tier": spec.tier,
                "production_path": spec.production_path,
                "ok": ok,
                "error": error,
                "role": spec.role,
            }
        )

    failures = [item for item in results if not item["ok"]]

    return {
        "success": not failures,
        "agents": results,
        "failures": failures,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(validate_agent_registry(), indent=2))
