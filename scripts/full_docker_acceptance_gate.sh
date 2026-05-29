#!/usr/bin/env bash
set -euo pipefail

run_prod_check_with_retry() {
  local attempts="${1:-3}"
  local stress="${2:-50}"
  local wait_seconds="${3:-10}"

  for i in $(seq 1 "$attempts"); do
    echo "Running prod_check attempt $i/$attempts..."
    if python prod_check.py --level full --environment docker_local --stress-requests-per-route "$stress"; then
      return 0
    fi

    echo "prod_check attempt $i failed. Waiting ${wait_seconds}s before retry..."
    sleep "$wait_seconds"
  done

  echo "❌ prod_check failed after $attempts attempts"
  return 1
}

run_update_with_recovery() {
  local label="$1"
  shift

  for i in 1 2 3; do
    echo "Running update [$label] attempt $i/3..."

    if python update.py "$@"; then
      return 0
    fi

    echo "⚠️ update.py reported failure for [$label]. Waiting for Docker/Nginx/backend settling..."
    sleep 15

    echo "Skipping global prod_check during update recovery because temporary routes may be active."
    echo "Retrying the same update instead..."
  done

  echo "❌ update failed after retries: $label"
  return 1
}

settle_gateway() {
  echo "Waiting 30s for Docker/Nginx/backends to settle..."
  sleep 30

  echo "Warming up all routes..."
  for path in / /api/ /users/ /orders/ /payments/ /reports/ /inventory/; do
    for i in 1 2 3; do
      curl -fsS --max-time 5 "http://127.0.0.1:8090${path}" >/dev/null || true
    done
  done
}

final_prod_check_or_heal() {
  local stress="${1:-50}"

  settle_gateway

  if run_prod_check_with_retry 3 "$stress" 15; then
    return 0
  fi

  echo "⚠️ Final prod_check failed after retries. Running Docker self-healing..."
  python heal.py --verify full

  settle_gateway

  echo "Re-running final production gate after healing..."
  run_prod_check_with_retry 3 "$stress" 15
}





PROJECT_DIR="/mnt/c/Users/offic/dev/pingora-generator"
LOCK_FILE="$PROJECT_DIR/tmp/auto_monitor_heal.lock"
START_CONFIG="$PROJECT_DIR/tmp/full_acceptance_start_config.json"

cd "$PROJECT_DIR"
source .venv/bin/activate

mkdir -p "$PROJECT_DIR/tmp"

exec 9>"$LOCK_FILE"

if ! flock -n 9; then
  echo "❌ Another monitor/healing run is active. Try again later."
  exit 1
fi

echo ""


echo "================================================================================"
echo "🧪 AI Pingora Docker full acceptance gate"
echo "================================================================================"

echo ""
echo "1) Saving starting config..."
cp generated-pingora-proxy/config.json "$START_CONFIG"

echo ""
echo "2) Checking agent architecture..."
python tools/check_agent_architecture.py

echo ""
echo "3) Checking algorithm integrity..."
python tools/algorithm_integrity.py check

echo ""
echo "4) Running baseline Docker production gate..."
run_prod_check_with_retry 3 20 10

echo ""
echo "5) Testing ADD route..."
run_update_with_recovery "add /e2eadd" --runtime docker_host --strategy bluegreen --verify full \
  "add /e2eadd to backend 127.0.0.1:9401"

curl -fsS http://127.0.0.1:8090/e2eadd/ | grep "9401" >/dev/null
echo "✅ add route passed"

echo ""
echo "6) Testing EDIT algorithm on /users and restoring it..."

ORIGINAL_USERS_ALGO="$(python - <<'PY'
import json
config=json.load(open("generated-pingora-proxy/config.json"))
for r in config["routes"]:
    if r.get("path")=="/users":
        print(r.get("balancing") or "single")
        break
PY
)"

if [ "$ORIGINAL_USERS_ALGO" = "random" ]; then
  TEMP_USERS_ALGO="round robin"
  TEMP_EXPECTED="round_robin"
else
  TEMP_USERS_ALGO="random"
  TEMP_EXPECTED="random"
fi

run_update_with_recovery "edit /users algorithm" --runtime docker_host --strategy bluegreen --verify full \
  "set /users algorithm to $TEMP_USERS_ALGO"

python - <<PY
import json, sys
config=json.load(open("generated-pingora-proxy/config.json"))
expected="$TEMP_EXPECTED"
for r in config["routes"]:
    if r.get("path")=="/users":
        actual=r.get("balancing")
        print("/users after edit:", actual)
        if actual != expected:
            raise SystemExit(f"Expected /users {expected}, got {actual}")
PY

run_update_with_recovery "restore /users algorithm" --runtime docker_host --strategy bluegreen --verify full \
  "set /users algorithm to $ORIGINAL_USERS_ALGO"

python - <<PY
import json, sys
config=json.load(open("generated-pingora-proxy/config.json"))
expected="$ORIGINAL_USERS_ALGO"
for r in config["routes"]:
    if r.get("path")=="/users":
        actual=r.get("balancing")
        print("/users restored:", actual)
        if actual != expected:
            raise SystemExit(f"Expected /users restored to {expected}, got {actual}")
PY

echo "✅ edit algorithm and restore passed"

echo ""
echo "7) Testing REMOVE route synonym..."
run_update_with_recovery "add /e2eremove" --runtime docker_host --strategy bluegreen --verify full \
  "add /e2eremove to backend 127.0.0.1:9401"

run_update_with_recovery "remove /e2eremove" --runtime docker_host --strategy bluegreen --verify full \
  --confirm-route-mutation \
  "remove /e2eremove"

if curl -fsS http://127.0.0.1:8090/ | grep "/e2eremove" >/dev/null; then
  echo "❌ /e2eremove still appears on homepage"
  exit 1
fi

echo "✅ remove route passed"

echo ""
echo "8) Testing DELETE route..."
run_update_with_recovery "delete /e2eadd" --runtime docker_host --strategy bluegreen --verify full \
  --confirm-route-mutation \
  "delete /e2eadd"

if curl -fsS http://127.0.0.1:8090/ | grep "/e2eadd" >/dev/null; then
  echo "❌ /e2eadd still appears on homepage"
  exit 1
fi

echo "✅ delete route passed"

echo ""
echo "9) Testing ROLLBACK restore from saved config..."

run_update_with_recovery "add /e2erollback" --runtime docker_host --strategy bluegreen --verify full \
  "add /e2erollback to backend 127.0.0.1:9401"

curl -fsS http://127.0.0.1:8090/e2erollback/ | grep "9401" >/dev/null

python - <<'PY'
import json
from pathlib import Path

from core.environment_deployer import (
    apply_profiles,
    start_docker_bluegreen,
    verify_deployment,
    write_project_from_config,
)

project = Path("/mnt/c/Users/offic/dev/pingora-generator")
start_config = project / "tmp" / "full_acceptance_start_config.json"

config = json.loads(start_config.read_text(encoding="utf-8"))

fixed = apply_profiles(
    config,
    runtime_mode="docker_host",
    strategy="bluegreen",
)

write_project_from_config(fixed)
deploy = start_docker_bluegreen(fixed)

verification = verify_deployment(
    fixed,
    live_url=deploy["live_url"],
    level="full",
)

print("Rollback verification success:", verification.get("success"))

if not verification.get("success"):
    raise SystemExit(1)
PY

if curl -fsS http://127.0.0.1:8090/ | grep "/e2erollback" >/dev/null; then
  echo "❌ rollback failed: /e2erollback still appears"
  exit 1
fi

echo "✅ rollback restore passed"
echo "Waiting 10s for Docker/Nginx to settle after rollback..."
sleep 10

echo ""
echo "10) Running final algorithm integrity check..."
python tools/algorithm_integrity.py check

echo ""
echo "11) Running final Docker production gate..."
final_prod_check_or_heal 50

echo ""
echo "12) Testing Docker self-healing..."
./scripts/stop_docker_bluegreen.sh

python heal.py --verify full

python - <<'PY'
import sqlite3
from pathlib import Path

db = Path("data/reliability_reports.sqlite3")
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row

row = conn.execute("""
    SELECT id, created_at_utc, runtime, strategy, root_cause, action, success
    FROM docker_self_healing_runs
    ORDER BY id DESC
    LIMIT 1
""").fetchone()

if not row:
    raise SystemExit("No docker_self_healing_runs row found")

print(dict(row))

if int(row["success"]) != 1:
    raise SystemExit("Latest Docker healing run did not store success=1")
PY

echo ""
echo "13) Running final post-healing production gate..."
final_prod_check_or_heal 50






echo ""
echo "================================================================================"
echo "✅ ALL AI PINGORA DOCKER ACCEPTANCE TESTS PASSED"
echo "================================================================================"
