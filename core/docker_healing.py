from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
UTC = timezone.utc
from pathlib import Path
from typing import Any

from core import environment_deployer as env


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "generated-pingora-proxy" / "config.json"
DB_PATH = PROJECT_ROOT / "data" / "reliability_reports.sqlite3"
DEFAULT_LIVE_URL = "http://127.0.0.1:8090/"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def run_best_effort(command: list[str], timeout: int = 30) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        return result.stdout or ""
    except Exception as exc:
        return f"Failed to run {' '.join(command)}: {exc}"


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Missing config file: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))

    if not isinstance(data, dict):
        raise ValueError("Config must be a JSON object")

    return data


def docker_ps_names() -> list[str]:
    out = run_best_effort(["docker", "ps", "-a", "--format", "{{.Names}}"], timeout=20)
    return sorted({line.strip() for line in out.splitlines() if line.strip()})


def collect_docker_healing_evidence() -> dict[str, Any]:
    compose_file = PROJECT_ROOT / "docker-bluegreen" / "docker-compose.yml"

    evidence: dict[str, Any] = {
        "collected_at_utc": utc_now(),
        "docker_ps": run_best_effort(["docker", "ps", "-a"], timeout=20),
        "docker_images": run_best_effort(["docker", "images"], timeout=20),
        "docker_networks": run_best_effort(["docker", "network", "ls"], timeout=20),
        "edge_logs": run_best_effort(
            ["docker", "logs", "--tail", "200", "ai-pingora-gateway-edge"],
            timeout=30,
        ),
        "blue_logs": run_best_effort(
            ["docker", "logs", "--tail", "200", "ai-pingora-gateway-blue"],
            timeout=30,
        ),
        "green_logs": run_best_effort(
            ["docker", "logs", "--tail", "200", "ai-pingora-gateway-green"],
            timeout=30,
        ),
    }

    if compose_file.exists():
        evidence["compose_ps"] = run_best_effort(
            ["docker", "compose", "-f", str(compose_file), "ps", "-a"],
            timeout=30,
        )
        evidence["compose_logs"] = run_best_effort(
            ["docker", "compose", "-f", str(compose_file), "logs", "--tail", "200"],
            timeout=60,
        )

    return evidence


def classify_docker_healing_root_cause(
    evidence: dict[str, Any],
    missing_containers: list[str],
) -> str:
    text = json.dumps(evidence, default=str).lower()

    if missing_containers:
        return "docker_bluegreen_stack_not_running"

    if "cannot connect to the docker daemon" in text:
        return "docker_daemon_unavailable"

    if "docker: command not found" in text:
        return "docker_cli_unavailable"

    if "exited" in text or "unhealthy" in text or "restart" in text:
        return "docker_container_unhealthy_or_exited"

    if "host.docker.internal" in text and (
        "connection refused" in text
        or "name or service not known" in text
        or "failed to lookup" in text
    ):
        return "docker_upstream_connectivity_failure"

    if "502" in text or "bad gateway" in text:
        return "docker_gateway_bad_gateway_upstream_failure"

    return "docker_bluegreen_runtime_or_verification_failure"


def route_paths(config: dict[str, Any]) -> list[str]:
    paths: list[str] = []

    for route in config.get("routes", []) or []:
        if not isinstance(route, dict):
            continue

        path = str(route.get("path") or "/").strip() or "/"

        if not path.startswith("/"):
            path = "/" + path

        paths.append(path)

    return sorted(set(paths), key=lambda p: (p != "/", p))


def warm_gateway_routes(
    live_url: str,
    config: dict[str, Any],
    rounds: int = 3,
    timeout: int = 5,
) -> None:
    base = live_url.rstrip("/")

    for _ in range(rounds):
        for path in route_paths(config):
            url_path = path
            if path != "/" and not path.endswith("/"):
                url_path = path + "/"

            url = base + url_path

            try:
                req = urllib.request.Request(
                    url,
                    method="GET",
                    headers={"User-Agent": "ai-pingora-healing-warmup"},
                )
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    response.read(256)
            except Exception:
                pass


def settle_and_warm(
    live_url: str,
    config: dict[str, Any],
    settle_seconds: int,
) -> None:
    if settle_seconds > 0:
        print(f"⏳ Waiting {settle_seconds}s for Docker/Nginx/backends to settle...")
        time.sleep(settle_seconds)

    print("🔥 Warming gateway routes before verification...")
    warm_gateway_routes(live_url, config, rounds=3, timeout=5)


def verify_with_retry(
    config: dict[str, Any],
    live_url: str,
    verify_level: str,
    attempts: int = 3,
    settle_seconds: int = 25,
) -> dict[str, Any]:
    last: dict[str, Any] = {
        "success": False,
        "error": "verification_not_run",
    }

    for attempt in range(1, attempts + 1):
        print("")
        print(f"🧪 Docker healing verification attempt {attempt}/{attempts}")

        settle_and_warm(live_url, config, settle_seconds if attempt == 1 else 15)

        try:
            verification = env.verify_deployment(
                config,
                live_url=live_url,
                level=verify_level,
            )
        except Exception as exc:
            verification = {
                "success": False,
                "error": repr(exc),
                "failure_summary": repr(exc),
            }

        last = verification if isinstance(verification, dict) else {
            "success": False,
            "raw_verification": str(verification),
        }

        if last.get("success"):
            print("✅ Docker healing verification passed")
            return last

        summary = (
            last.get("failure_summary")
            or last.get("error")
            or json.dumps(last, default=str)[:500]
        )
        print(f"⚠️ Verification attempt {attempt} failed: {summary}")

    print("❌ Docker healing verification failed after retries")
    return last


def call_optional_demo_backend_starter(config: dict[str, Any]) -> Any:
    """
    Keep this best-effort because different project versions may expose the
    demo-backend starter under different names.
    """

    candidates = [
        "start_demo_backends",
        "start_demo_backends_for_config",
        "ensure_demo_backends",
        "start_required_demo_backends",
    ]

    errors: list[str] = []

    for name in candidates:
        fn = getattr(env, name, None)

        if not callable(fn):
            continue

        try:
            return fn(config)
        except TypeError:
            try:
                return fn(config=config)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        except Exception as exc:
            errors.append(f"{name}: {exc}")

    if errors:
        return {"started": False, "errors": errors}

    return {"started": False, "reason": "no_demo_backend_starter_found"}


def ensure_storage() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS docker_self_healing_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at_utc TEXT NOT NULL,
                completed_at_utc TEXT NOT NULL,
                runtime TEXT,
                strategy TEXT,
                root_cause TEXT,
                action TEXT,
                success INTEGER NOT NULL,
                before_containers_json TEXT,
                after_containers_json TEXT,
                details_json TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def store_docker_healing_result(result: dict[str, Any]) -> None:
    ensure_storage()

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """
            INSERT INTO docker_self_healing_runs (
                created_at_utc,
                completed_at_utc,
                runtime,
                strategy,
                root_cause,
                action,
                success,
                before_containers_json,
                after_containers_json,
                details_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.get("created_at_utc") or utc_now(),
                result.get("completed_at_utc") or utc_now(),
                result.get("runtime"),
                result.get("strategy"),
                result.get("root_cause"),
                result.get("action"),
                1 if result.get("success") else 0,
                json.dumps(result.get("before_containers", []), default=str),
                json.dumps(result.get("after_containers", []), default=str),
                json.dumps(result, default=str),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def heal_docker_bluegreen(
    verify_level: str = "full",
    config_path: Path = CONFIG_PATH,
) -> dict[str, Any]:
    created_at = utc_now()

    print("")
    print("🩺 Docker blue-green self-healing")

    config = load_config(config_path)

    runtime = str(config.get("runtime") or config.get("runtime_mode") or "docker_host")
    strategy = str(
        config.get("strategy")
        or config.get("deployment_strategy")
        or "bluegreen"
    )

    print(f"Runtime: {runtime}")
    print(f"Strategy: {strategy}")

    before_containers = docker_ps_names()

    required = {
        "ai-pingora-gateway-blue",
        "ai-pingora-gateway-green",
        "ai-pingora-gateway-edge",
    }

    missing = sorted(required - set(before_containers))

    before_evidence = collect_docker_healing_evidence()
    root_cause = classify_docker_healing_root_cause(before_evidence, missing)

    print(f"Root cause: {root_cause}")

    result: dict[str, Any] = {
        "created_at_utc": created_at,
        "completed_at_utc": utc_now(),
        "runtime": runtime,
        "strategy": strategy,
        "root_cause": root_cause,
        "action": "redeploy_docker_bluegreen_then_verify_with_retry",
        "success": False,
        "healed": False,
        "before_containers": before_containers,
        "missing_before": missing,
        "before_evidence": before_evidence,
        "verify_level": verify_level,
    }

    try:
        fixed = env.apply_profiles(
            config,
            runtime_mode="docker_host",
            strategy="bluegreen",
        )

        env.write_project_from_config(fixed)

        backend_result = call_optional_demo_backend_starter(fixed)
        result["demo_backends"] = backend_result

        deploy_result = env.start_docker_bluegreen(fixed)
        result["deploy_result"] = deploy_result

        live_url = (
            deploy_result.get("live_url")
            if isinstance(deploy_result, dict)
            else None
        ) or fixed.get("live_url") or DEFAULT_LIVE_URL

        verification = verify_with_retry(
            fixed,
            live_url=live_url,
            verify_level=verify_level,
            attempts=3,
            settle_seconds=25,
        )

        after_containers = docker_ps_names()
        after_evidence = collect_docker_healing_evidence()

        success = bool(verification.get("success"))

        result.update(
            {
                "completed_at_utc": utc_now(),
                "success": success,
                "healed": success,
                "live_url": live_url,
                "verification": verification,
                "after_containers": after_containers,
                "after_evidence": after_evidence,
            }
        )

        store_docker_healing_result(result)

        if success:
            print("")
            print("✅ Docker blue-green self-healing succeeded")
        else:
            print("")
            print("❌ Docker blue-green self-healing failed after retries")

        return result

    except Exception as exc:
        result.update(
            {
                "completed_at_utc": utc_now(),
                "success": False,
                "healed": False,
                "error": repr(exc),
                "after_containers": docker_ps_names(),
                "after_evidence": collect_docker_healing_evidence(),
            }
        )

        store_docker_healing_result(result)

        print("")
        print(f"❌ Docker blue-green self-healing crashed: {exc}")

        return result


# Compatibility aliases for existing heal.py versions.
run_docker_bluegreen_healing = heal_docker_bluegreen
run_docker_healing = heal_docker_bluegreen
heal = heal_docker_bluegreen
run = heal_docker_bluegreen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", default="full")
    args = parser.parse_args(argv)

    result = heal_docker_bluegreen(verify_level=args.verify)

    print("")
    print(json.dumps(result, indent=2, default=str))

    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
