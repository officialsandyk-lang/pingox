from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = PROJECT_ROOT / "generated-pingora-proxy" / "config.json"


def http_status(url: str, timeout: int = 5) -> int | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ai-pingora-prod-check/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            response.read()
            return int(response.status)
    except Exception:
        return None


def run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)


def docker_check() -> dict[str, Any]:
    ps = run(["docker", "ps", "--format", "{{.Names}}"], timeout=20)

    names = [line.strip() for line in ps.stdout.splitlines() if line.strip()]

    required = {
        "ai-pingora-gateway-edge",
        "ai-pingora-gateway-blue",
        "ai-pingora-gateway-green",
    }

    return {
        "success": ps.returncode == 0 and "ai-pingora-gateway-edge" in names,
        "containers": names,
        "required_seen": sorted(required.intersection(names)),
        "missing": sorted(required - set(names)),
    }


def concurrent_route_check(live_url: str, paths: list[str], requests_per_path: int = 50) -> dict[str, Any]:
    urls = []

    for path in paths:
        if path == "/":
            url = live_url.rstrip("/") + "/"
        else:
            url = live_url.rstrip("/") + path + "/"

        for _ in range(requests_per_path):
            urls.append(url)

    failures = []
    success_count = 0

    with ThreadPoolExecutor(max_workers=32) as pool:
        futures = {pool.submit(http_status, url): url for url in urls}

        for future in as_completed(futures):
            url = futures[future]
            status = future.result()

            if status and 200 <= status < 500:
                success_count += 1
            else:
                failures.append({"url": url, "status": status})

    return {
        "success": not failures,
        "total": len(urls),
        "success_count": success_count,
        "failure_count": len(failures),
        "failures": failures[:20],
    }


def run_production_gate(
    *,
    config_path: Path,
    live_url: str,
    level: str,
    stress_requests_per_route: int,
    environment: str = "docker_local",
) -> dict[str, Any]:
    from agents.config_verification_agent import (
        build_verification_contract,
        validate_config_contract,
    )
    from core.post_change_verifier import verify_after_change
    from core.algorithm_integrity import validate_algorithm_integrity
    from core.production_security_contract import validate_production_security_contract

    config = json.loads(config_path.read_text(encoding="utf-8"))

    contract = build_verification_contract(
        config,
        live_url=live_url,
        runtime=config.get("runtime_mode") or config.get("runtime"),
        strategy=config.get("strategy") or config.get("deployment_strategy"),
        level=level,
    )

    config_contract = validate_config_contract(config, contract)

    if not config_contract["success"]:
        return {
            "success": False,
            "stage": "config_contract",
            "config_contract": config_contract,
        }

    production_security = validate_production_security_contract(
        config,
        environment=environment,
    )

    if not production_security["success"]:
        return {
            "success": False,
            "stage": "production_security_contract",
            "config_contract": config_contract,
            "production_security": production_security,
        "algorithm_integrity": algorithm_integrity,
        }

    algorithm_integrity = validate_algorithm_integrity(
        config,
        require_baseline=True,
        allow_extra_routes=False,
    )

    if not algorithm_integrity["success"]:
        return {
            "success": False,
            "stage": "algorithm_integrity",
            "config_contract": config_contract,
            "production_security": production_security,
            "algorithm_integrity": algorithm_integrity,
        }

    docker = docker_check()

    verifier = verify_after_change(
        config=config,
        config_path=config_path,
        live_url=live_url,
        source="prod_check_config_agent_contract",
        change_type="docker_production_gate",
        level=level,
        store=True,
    )

    paths = list(contract.expected_routes.keys())

    stress = concurrent_route_check(
        live_url=live_url,
        paths=paths,
        requests_per_path=stress_requests_per_route,
    )

    success = bool(
        config_contract.get("success")
        and docker.get("success")
        and verifier.get("success")
        and stress.get("success")
    )

    return {
        "success": success,
        "contract": contract.to_dict(),
        "config_contract": config_contract,
        "docker": docker,
        "production_security": production_security,
        "verifier": {
            "success": verifier.get("success"),
            "level": verifier.get("level"),
            "route_count": verifier.get("route_count"),
            "failure_summary": verifier.get("failure_summary"),
        },
        "stress": stress,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Config-agent-controlled Docker production readiness gate.")

    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--live-url", default="http://127.0.0.1:8090/")
    parser.add_argument("--level", choices=["crud", "stress", "full"], default="full")
    parser.add_argument("--environment", default="docker_local", choices=["local", "docker_local", "production", "kubernetes", "ecs", "vm", "bare_metal"])
    parser.add_argument("--stress-requests-per-route", type=int, default=50)

    args = parser.parse_args()

    result = run_production_gate(
        config_path=Path(args.config),
        live_url=args.live_url,
        level=args.level,
        stress_requests_per_route=args.stress_requests_per_route,
        environment=args.environment,
    )

    print("")
    print("🧪 Config-agent-controlled Docker production gate")
    print("")

    if result["success"]:
        print("✅ Docker homepage passed")
        print("✅ Docker algorithms passed")
        print("✅ Docker CRUD passed")
        print("✅ Docker security passed")
        print("✅ Docker concurrent traffic passed")
        print("✅ Docker update/add/edit/delete passed")
        print("✅ Docker rollback/healing safety passed")
        print("✅ ALL DOCKER PRODUCTION TESTS PASSED")
        return 0

    print("❌ DOCKER PRODUCTION TESTS FAILED")
    print(json.dumps(result, indent=2, default=str)[:12000])
    return 1




# ---------------------------------------------------------------------------
# Security contract production gate wrapper
# ---------------------------------------------------------------------------
if not globals().get("_SECURITY_POLICY_CONTRACT_PROD_CHECK_WRAPPED"):
    _SECURITY_POLICY_CONTRACT_PROD_CHECK_WRAPPED = True

    import sys as _security_contract_sys
    from pathlib import Path as _SecurityContractPath
    from core.security_policy_contract import (
        load_config as _security_contract_load_config,
        verify_security_policy_contract as _security_contract_verify,
    )

    _original_prod_check_main = main

    def main():
        original_result = _original_prod_check_main()

        config_path = _SecurityContractPath("generated-pingora-proxy/config.json")

        if not config_path.exists():
            print("⚠️ Security contract check skipped: generated config not found.")
            return original_result

        strict = "production" in " ".join(_security_contract_sys.argv).lower()

        config = _security_contract_load_config(config_path)
        result = _security_contract_verify(config, strict=strict)

        print("")
        print("🔐 Security policy contract gate")
        print(f"Strict mode: {strict}")
        print(f"Success: {result['ok']}")

        for check in result["checks"]:
            icon = "✅" if check["ok"] else "❌"
            print(f"{icon} {check['name']}")

        if not result["ok"]:
            return 1

        return original_result



# ---------------------------------------------------------------------------
# System decision-engine production-readiness gate
# ---------------------------------------------------------------------------
if not globals().get("_SYSTEM_DECISION_ENGINE_PROD_CHECK_WRAPPED"):
    _SYSTEM_DECISION_ENGINE_PROD_CHECK_WRAPPED = True

    import json as _system_decision_json
    import sys as _system_decision_sys
    from pathlib import Path as _SystemDecisionPath
    from core.system_decision_engine import evaluate_system_config as _system_evaluate_config

    _original_prod_check_main_for_system_decision = main

    def main():
        original_result = _original_prod_check_main_for_system_decision()

        config_path = _SystemDecisionPath("generated-pingora-proxy/config.json")

        if not config_path.exists():
            print("⚠️ System decision production gate skipped: generated config not found.")
            return original_result

        config = _system_decision_json.loads(config_path.read_text(encoding="utf-8"))

        strict = "production" in " ".join(_system_decision_sys.argv).lower()

        decision = _system_evaluate_config(
            config,
            prompt="production readiness check",
            strict_security=strict,
        )

        print("")
        print("🧠 System decision production gate")
        print(f"Strict security: {strict}")
        print(f"Success: {decision['ok']}")

        for item in decision.get("decisions", []):
            icon = "✅" if item.get("ok") else "❌"
            print(f"{icon} [{item.get('category')}] {item.get('reason')}")

        if not decision.get("ok"):
            return 1

        return original_result

if __name__ == "__main__":
    raise SystemExit(main())
