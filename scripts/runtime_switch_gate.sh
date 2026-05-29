#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/mnt/c/Users/offic/dev/pingora-generator"
cd "$PROJECT_DIR"
source .venv/bin/activate

mkdir -p tmp

echo ""
echo "================================================================================"
echo "🧪 AI Pingora runtime switch gate: Docker → Local → Docker"
echo "================================================================================"

restore_docker() {
  echo ""
  echo "🐳 Restoring Docker blue/green runtime..."

  python - <<'PY'
import json
from pathlib import Path
from typing import Any

from core.environment_deployer import (
    apply_profiles,
    start_docker_bluegreen,
    write_project_from_config,
)
from core.update_verification_retry import verify_deployment_with_retry

PROJECT = Path("/mnt/c/Users/offic/dev/pingora-generator")
CONFIG_PATH = PROJECT / "generated-pingora-proxy" / "config.json"

def to_docker(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: to_docker(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_docker(v) for v in value]
    if isinstance(value, str):
        return value.replace("127.0.0.1:", "host.docker.internal:")
    return value

config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
config = to_docker(config)

config["runtime"] = "docker_host"
config["runtime_mode"] = "docker_host"
config["strategy"] = "bluegreen"
config["deployment_strategy"] = "bluegreen"
config["live_url"] = "http://127.0.0.1:8090/"

fixed = apply_profiles(
    config,
    runtime_mode="docker_host",
    strategy="bluegreen",
)

write_project_from_config(fixed)

(PROJECT / "tmp" / "runtime_switch_start_config.json").write_text(
    json.dumps(fixed, indent=2),
    encoding="utf-8",
)

deploy = start_docker_bluegreen(fixed)

verification = verify_deployment_with_retry(
    fixed,
    live_url=deploy["live_url"],
    level="full",
    attempts=3,
    first_wait_seconds=25,
    retry_wait_seconds=15,
)

print("Docker verification success:", verification.get("success"))

if not verification.get("success"):
    raise SystemExit(1)
PY
}

stop_local() {
  echo "🛑 Stopping local gateway..."
  python - <<'PY' || true
from pathlib import Path
try:
    from agents.runtime_agent import stop_local_gateway
    print(stop_local_gateway(8090, Path("/mnt/c/Users/offic/dev/pingora-generator/generated-pingora-proxy")))
except Exception as exc:
    print(f"runtime_agent stop skipped: {exc}")
PY

  lsof -ti TCP:8090 | xargs -r kill -9 || true
  pkill -f "generated-pingora-proxy/target/release" || true
  pkill -f "generated-pingora-proxy/target/debug" || true
  pkill -f "cargo run" || true
  sleep 3
}

cleanup() {
  echo ""
  echo "Cleanup: restoring Docker runtime..."
  stop_local
  restore_docker || true
}

trap cleanup EXIT

echo ""
echo "1) Normalising and confirming Docker baseline..."
restore_docker

echo ""
echo "2) Docker production gate before switch..."
python prod_check.py --level full --environment docker_local --stress-requests-per-route 30

echo ""
echo "3) Stopping Docker blue/green before local runtime..."
./scripts/stop_docker_bluegreen.sh || true
sleep 3

echo ""
echo "4) Clearing local port..."
stop_local

echo ""
echo "5) Creating deterministic LOCAL runtime config..."
python - <<'PY'
import json
from pathlib import Path
from typing import Any

from core.environment_deployer import write_project_from_config

PROJECT = Path("/mnt/c/Users/offic/dev/pingora-generator")
CONFIG_PATH = PROJECT / "tmp" / "runtime_switch_start_config.json"

def to_local(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: to_local(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_local(v) for v in value]
    if isinstance(value, str):
        return value.replace("host.docker.internal:", "127.0.0.1:")
    return value

config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
config = to_local(config)

config["runtime"] = "local"
config["runtime_mode"] = "local"
config["strategy"] = "direct"
config["deployment_strategy"] = "direct"
config["live_url"] = "http://127.0.0.1:8090/"

write_project_from_config(config)

generated = PROJECT / "generated-pingora-proxy"
config_text = (generated / "config.json").read_text(encoding="utf-8")
main_text = (generated / "src" / "main.rs").read_text(encoding="utf-8")

if "host.docker.internal" in config_text or "host.docker.internal" in main_text:
    raise SystemExit("Local config still contains host.docker.internal")

print("✅ Local config generated with 127.0.0.1 upstreams")
PY

echo ""
echo "6) Starting demo backends for local runtime..."
python scripts/demo_backends.py start generated-pingora-proxy/config.json

echo ""
echo "7) Starting local Pingora runtime with runtime_agent..."
python - <<'PY'
import json
from pathlib import Path

from agents.runtime_agent import run_local_gateway
from core.update_verification_retry import verify_deployment_with_retry

PROJECT = Path("/mnt/c/Users/offic/dev/pingora-generator")
GENERATED = PROJECT / "generated-pingora-proxy"
CONFIG = json.loads((GENERATED / "config.json").read_text(encoding="utf-8"))

result = run_local_gateway(
    GENERATED,
    port=8090,
    startup_timeout_seconds=300,
    stop_existing=True,
)

print(json.dumps(result, indent=2, default=str))

if not result.get("success") and not result.get("ok"):
    log_file = result.get("log_file")
    if log_file:
        p = Path(log_file)
        if p.exists():
            print("")
            print("Local gateway log tail:")
            print(p.read_text(encoding="utf-8", errors="replace")[-5000:])
    raise SystemExit(1)

verification = verify_deployment_with_retry(
    CONFIG,
    live_url="http://127.0.0.1:8090/",
    level="full",
    attempts=3,
    first_wait_seconds=20,
    retry_wait_seconds=15,
)

print("Local verification success:", verification.get("success"))

if not verification.get("success"):
    raise SystemExit(1)
PY

echo ""
echo "8) Local smoke checks..."
curl -fsS http://127.0.0.1:8090/ >/dev/null
curl -fsS http://127.0.0.1:8090/api/ >/dev/null
curl -fsS http://127.0.0.1:8090/users/ >/dev/null
curl -fsS http://127.0.0.1:8090/orders/ >/dev/null
curl -fsS http://127.0.0.1:8090/payments/ >/dev/null
curl -fsS http://127.0.0.1:8090/reports/ >/dev/null
curl -fsS http://127.0.0.1:8090/inventory/ >/dev/null
echo "✅ Local runtime passed"

echo ""
echo "9) Stopping local runtime..."
stop_local

echo ""
echo "10) Restoring Docker runtime..."
restore_docker

echo ""
echo "11) Final algorithm integrity check..."
python tools/algorithm_integrity.py check

echo ""
echo "12) Final Docker production gate with retry..."
for attempt in 1 2 3; do
  echo ""
  echo "Final Docker prod_check attempt $attempt/3..."

  if python prod_check.py --level full --environment docker_local --stress-requests-per-route 50; then
    echo "✅ Final Docker production gate passed"
    break
  fi

  if [ "$attempt" = "3" ]; then
    echo "❌ Final Docker production gate failed after retries"
    exit 1
  fi

  echo "⚠️ Final Docker prod_check had transient failure. Waiting 20s before retry..."
  sleep 20
done

trap - EXIT

echo ""
echo "================================================================================"
echo "✅ RUNTIME SWITCH GATE PASSED: Docker → Local → Docker is stable"
echo "================================================================================"
