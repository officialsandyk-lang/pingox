from __future__ import annotations

import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "generated-pingora-proxy" / "config.json"


def load_runtime_strategy() -> tuple[str, str]:
    if not CONFIG_PATH.exists():
        return "local", "direct"

    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return "local", "direct"

    runtime = str(config.get("runtime_mode") or config.get("runtime") or "local")
    strategy = str(config.get("strategy") or config.get("deployment_strategy") or "direct")

    return runtime, strategy


def main() -> int:
    runtime, strategy = load_runtime_strategy()

    runtime = runtime.lower().replace("-", "_")
    strategy = strategy.lower().replace("-", "").replace("_", "")

    if runtime in {"docker", "docker_host"} or strategy == "bluegreen":
        from core.docker_healing import main as docker_healing_main

        print("🩺 heal.py selected Docker blue-green healing path")
        return docker_healing_main()

    from core.log_healing import main as local_healing_main

    print("🩺 heal.py selected local log-healing path")
    return local_healing_main()


if __name__ == "__main__":
    raise SystemExit(main())
