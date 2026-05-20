from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).parent.absolute()
PROJECT_DIR = PROJECT_ROOT / "generated-pingora-proxy"

os.chdir(PROJECT_ROOT)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from orchestration.graph import run_graph
from core.post_change_verifier import format_verification_report, verify_after_change
from core.deployment_prepare import prepare_config_for_deployment
from core.intent_contract import enforce_intent_contract, format_intent_contract_report
from core.security_profile import apply_local_production_security_profile


SUPPORTED_RUNTIMES = {
    "local",
    "docker",
    "docker_host",
    "kubernetes",
    "k8s",
    "ecs",
    "nomad",
    "vm",
    "bare_metal",
    "baremetal",
}

SUPPORTED_STRATEGIES = {
    "direct",
    "bluegreen",
    "rolling",
    "canary",
}


CURRENTLY_IMPLEMENTED = {
    ("local", "direct"),
    ("local", "bluegreen"),
    ("docker", "bluegreen"),
    ("docker_host", "bluegreen"),
}


def normalize_runtime(value: str | None) -> str:
    text = str(value or "local").strip().lower()
    text = text.replace("-", "_")

    aliases = {
        "host": "local",
        "native": "local",
        "local_host": "local",
        "dockerhost": "docker_host",
        "docker_host": "docker_host",
        "docker": "docker",
        "compose": "docker_host",
        "k8s": "kubernetes",
        "kube": "kubernetes",
        "kubernetes": "kubernetes",
        "ecs": "ecs",
        "nomad": "nomad",
        "vm": "vm",
        "baremetal": "bare_metal",
        "bare_metal": "bare_metal",
    }

    return aliases.get(text, text)


def normalize_strategy(value: str | None, runtime: str) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("-", "").replace("_", "")

    aliases = {
        "direct": "direct",
        "dev": "direct",
        "simple": "direct",
        "bluegreen": "bluegreen",
        "bg": "bluegreen",
        "rolling": "rolling",
        "canary": "canary",
    }

    if text:
        return aliases.get(text, text)

    if runtime == "local":
        return "direct"

    return "bluegreen"


def effective_runtime_mode(runtime: str, strategy: str) -> str:
    if runtime == "local" and strategy == "direct":
        return "local"

    if runtime in {"local", "docker", "docker_host"} and strategy == "bluegreen":
        return "docker_host"

    if runtime == "docker":
        return "docker_host"

    return runtime


def validate_runtime_strategy(runtime: str, strategy: str) -> None:
    if runtime not in SUPPORTED_RUNTIMES:
        allowed = ", ".join(sorted(SUPPORTED_RUNTIMES))
        raise SystemExit(f"❌ Unsupported runtime: {runtime}\nAllowed: {allowed}")

    if strategy not in SUPPORTED_STRATEGIES:
        allowed = ", ".join(sorted(SUPPORTED_STRATEGIES))
        raise SystemExit(f"❌ Unsupported strategy: {strategy}\nAllowed: {allowed}")

    if (runtime, strategy) not in CURRENTLY_IMPLEMENTED:
        print("")
        print("⚠️ Runtime/strategy accepted but not fully implemented yet.")
        print(f"Requested: runtime={runtime}, strategy={strategy}")
        print("")
        print("Currently implemented:")
        print("- local + direct")
        print("- local + bluegreen using Docker-based blue/green")
        print("- docker/docker_host + bluegreen")
        print("")
        print("This is reserved for future ADN deployment support.")
        raise SystemExit(2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="AI Pingora Gateway generator",
    )

    parser.add_argument(
        "--runtime",
        default="local",
        help="Runtime target: local, docker, docker_host, kubernetes, ecs, nomad, vm, bare_metal",
    )

    parser.add_argument(
        "--strategy",
        default=None,
        help="Deployment strategy: direct, bluegreen, rolling, canary",
    )

    parser.add_argument(
        "--verify",
        choices=["none", "smoke", "algorithm", "crud", "stress", "full"],
        default="none",
        help="Verification level after local deployment. Use stress for full local proof.",
    )

    parser.add_argument(
        "prompt",
        nargs="*",
        help="Natural-language gateway prompt",
    )

    return parser.parse_args()


def run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd) if cwd else str(PROJECT_ROOT),
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def read_config() -> dict[str, Any]:
    config_path = PROJECT_DIR / "config.json"

    if not config_path.exists():
        raise FileNotFoundError(f"Missing generated config: {config_path}")

    return json.loads(config_path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def sync_local_config_sources(config: dict[str, Any]) -> None:
    write_json(PROJECT_ROOT / "active_config.json", config)
    write_json(PROJECT_ROOT / "gateway_config.json", config)

    current_config = PROJECT_ROOT / "generated-projects" / "default-project" / "current_config.json"
    write_json(current_config, config)


def port_from_config(config: dict[str, Any]) -> int:
    try:
        return int(config.get("port") or config.get("listen_port") or config.get("proxy_port") or 8090)
    except Exception:
        return 8090


def live_url_for_config(config: dict[str, Any]) -> str:
    return f"http://127.0.0.1:{port_from_config(config)}/"


def kill_port(port: int) -> None:
    run_command(
        [
            "bash",
            "-lc",
            f"lsof -tiTCP:{int(port)} -sTCP:LISTEN | xargs -r kill -9",
        ],
        cwd=PROJECT_ROOT,
        timeout=20,
    )


def start_demo_backends(config_path: Path) -> None:
    script = PROJECT_ROOT / "scripts" / "demo_backends.py"

    if not script.exists():
        print("ℹ️ scripts/demo_backends.py not found; skipping demo backend start.")
        return

    result = run_command(
        [sys.executable, str(script), "start", str(config_path)],
        cwd=PROJECT_ROOT,
        timeout=90,
    )

    if result.stdout.strip():
        print(result.stdout.strip())

    if result.returncode != 0:
        raise RuntimeError(
            "Failed to start demo backends\n\n"
            f"STDOUT:\n{result.stdout}\n\n"
            f"STDERR:\n{result.stderr}"
        )


def start_local_gateway(config: dict[str, Any]) -> dict[str, Any]:
    port = port_from_config(config)

    runtime_dir = PROJECT_DIR / ".runtime"
    log_dir = PROJECT_DIR / "tmp" / "logs"

    runtime_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / f"local-pingora-{port}.log"

    print("")
    print("🚀 Starting local Pingora gateway...")
    print(f"Live URL: http://127.0.0.1:{port}/")
    print(f"Log file: {log_file}")

    kill_port(port)

    with log_file.open("a", encoding="utf-8", errors="replace") as log:
        process = subprocess.Popen(
            ["cargo", "run"],
            cwd=str(PROJECT_DIR),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            text=True,
            start_new_session=True,
        )

    (runtime_dir / f"local-pingora-{port}.pid").write_text(str(process.pid), encoding="utf-8")

    deadline = time.time() + 120

    while time.time() < deadline:
        if process.poll() is not None:
            tail = log_file.read_text(encoding="utf-8", errors="replace")[-5000:]
            raise RuntimeError(f"Local gateway exited before ready.\n\n{tail}")

        result = run_command(
            [
                "bash",
                "-lc",
                f"curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{port}/",
            ],
            cwd=PROJECT_ROOT,
            timeout=10,
        )

        status = result.stdout.strip()

        if status in {"200", "301", "302", "403", "404", "405"}:
            print(f"✅ Local gateway ready: HTTP {status}")
            return {
                "success": True,
                "pid": process.pid,
                "live_url": f"http://127.0.0.1:{port}/",
                "status": status,
                "log_file": str(log_file),
            }

        time.sleep(1)

    tail = log_file.read_text(encoding="utf-8", errors="replace")[-5000:] if log_file.exists() else ""
    raise RuntimeError(f"Local gateway did not become ready on port {port}.\n\n{tail}")


def run_post_deployment_verification(
    config: dict[str, Any],
    *,
    verify_level: str,
    live_url: str,
) -> dict[str, Any]:
    verify_level = (verify_level or "none").lower().strip()

    if verify_level == "none":
        return {
            "success": True,
            "skipped": True,
            "level": "none",
        }

    verification = verify_after_change(
        config=config,
        config_path=PROJECT_DIR / "config.json",
        live_url=live_url,
        source="main",
        change_type="fresh_deploy",
        level=verify_level,
        store=True,
    )

    print("")
    print(format_verification_report(verification))

    if not verification.get("success"):
        from core.auto_heal import try_auto_heal

        healing = try_auto_heal(
            verify_level=verify_level if verify_level != "none" else "full",
            reason="main_post_deployment_verification_failed",
            max_attempts=2,
        )

        if healing.get("success"):
            return {
                "success": True,
                "healed": True,
                "level": verify_level,
                "initial_verification": verification,
                "healing": healing,
                "failure_summary": None,
            }

        raise RuntimeError(
            "Post-deployment verification failed and auto-healing did not recover it: "
            + str(verification.get("failure_summary"))
        )

    return verification


def call_run_graph(prompt: str, runtime_mode: str, strategy: str) -> Any:
    """
    Compatibility wrapper for orchestration.graph.run_graph.

    Supports both old and new signatures, including:
      run_graph(prompt, project_root, project_dir)
      run_graph(prompt, project_root, project_dir, runtime=..., strategy=...)
      run_graph(prompt, runtime=..., strategy=...)
      run_graph(state_dict)
    """

    attempts = [
        lambda: run_graph(
            prompt,
            PROJECT_ROOT,
            PROJECT_DIR,
            runtime=runtime_mode,
            strategy=strategy,
        ),
        lambda: run_graph(
            prompt,
            PROJECT_ROOT,
            PROJECT_DIR,
            runtime_mode=runtime_mode,
            strategy=strategy,
        ),
        lambda: run_graph(
            prompt,
            PROJECT_ROOT,
            PROJECT_DIR,
            runtime=runtime_mode,
            deployment_strategy=strategy,
        ),
        lambda: run_graph(
            prompt,
            str(PROJECT_ROOT),
            str(PROJECT_DIR),
            runtime=runtime_mode,
            strategy=strategy,
        ),
        lambda: run_graph(
            prompt,
            str(PROJECT_ROOT),
            str(PROJECT_DIR),
        ),
        lambda: run_graph(
            prompt,
            PROJECT_ROOT,
            PROJECT_DIR,
        ),
        lambda: run_graph(
            prompt,
            runtime=runtime_mode,
            strategy=strategy,
        ),
        lambda: run_graph(
            prompt,
            runtime_mode=runtime_mode,
            strategy=strategy,
        ),
        lambda: run_graph(
            {
                "prompt": prompt,
                "update_prompt": prompt,
                "project_root": str(PROJECT_ROOT),
                "project_dir": str(PROJECT_DIR),
                "runtime": runtime_mode,
                "runtime_mode": runtime_mode,
                "strategy": strategy,
                "deployment_strategy": strategy,
            }
        ),
        lambda: run_graph(prompt),
    ]

    last_error: Exception | None = None

    for attempt in attempts:
        try:
            return attempt()
        except TypeError as exc:
            last_error = exc
            continue

    raise last_error or RuntimeError("Could not call orchestration.graph.run_graph")



# ---------------------------------------------------------------------
# Prompt intent contract enforcement
# ---------------------------------------------------------------------


def enforce_prompt_intent_or_repair(prompt: str, config: dict[str, Any]) -> dict[str, Any]:
    result = enforce_intent_contract(
        prompt=prompt,
        config=config,
        source="main",
        auto_repair=True,
        store=True,
    )

    print("")
    print(format_intent_contract_report(result))

    if not result.get("success"):
        failures = (result.get("validation") or {}).get("failures") or []
        raise RuntimeError(
            "Generated config does not match prompt intent and could not be repaired: "
            + "; ".join(
                f"{item.get('path')}:{item.get('code')}"
                for item in failures[:10]
            )
        )

    return result


# ---------------------------------------------------------------------
# End prompt intent contract enforcement


def main() -> int:
    load_dotenv()

    args = parse_args()

    prompt = " ".join(args.prompt).strip()

    if not prompt:
        print("❌ Please provide a natural-language gateway prompt.")
        return 2

    runtime = normalize_runtime(args.runtime)
    strategy = normalize_strategy(args.strategy, runtime)
    validate_runtime_strategy(runtime, strategy)

    runtime_mode = effective_runtime_mode(runtime, strategy)

    print("")
    print("🚦 AI Pingora Gateway")
    print(f"Runtime: {runtime}")
    print(f"Strategy: {strategy}")
    print(f"Runtime addressing mode: {runtime_mode}")
    print("")

    result = call_run_graph(prompt, runtime_mode, strategy)

    if result is None:
        result = {}

    if not isinstance(result, dict):
        result = {
            "success": True,
            "result": result,
        }

    success = bool(result.get("success", True))

    if not success:
        print("")
        print("❌ Gateway generation failed.")
        print(json.dumps(result, indent=2, default=str))
        return 1

    config = result.get("config")

    if not isinstance(config, dict):
        config = read_config()

    config["runtime"] = runtime_mode
    config["runtime_mode"] = runtime_mode
    config["strategy"] = strategy
    config["deployment_strategy"] = strategy

    config = apply_local_production_security_profile(config, environment=runtime)

    intent_result = enforce_prompt_intent_or_repair(prompt, config)
    config = intent_result["config"]
    result["intent_contract"] = intent_result

    prepared = prepare_config_for_deployment(
        prompt=prompt,
        config=config,
        runtime=runtime,
        runtime_mode=runtime_mode,
        strategy=strategy,
        project_dir=PROJECT_DIR,
        source="main",
    )
    config = prepared["config"]
    result["intent_contract"] = prepared["intent_contract"]

    write_json(PROJECT_DIR / "config.json", config)
    sync_local_config_sources(config)

    live_url = result.get("live_url") or live_url_for_config(config)

    if runtime == "local" and strategy == "direct":
        start_demo_backends(PROJECT_DIR / "config.json")
        gateway_result = start_local_gateway(config)
        live_url = gateway_result.get("live_url") or live_url

    verification = run_post_deployment_verification(
        config,
        verify_level=args.verify,
        live_url=live_url,
    )

    result["verification"] = verification
    result["live_url"] = live_url

    print("")
    print("✅ Project generated successfully")
    print(f"📁 Folder: {PROJECT_DIR}")
    print(f"🌐 Live URL: {live_url}")

    if args.verify != "none":
        print(f"🧪 Verification: {args.verify} ✅")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
