#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/mnt/c/Users/offic/dev/pingora-generator"
LOG_DIR="$PROJECT_DIR/data/monitor_logs"
LOCK_FILE="$PROJECT_DIR/tmp/auto_monitor_heal.lock"

mkdir -p "$LOG_DIR" "$PROJECT_DIR/tmp"

cd "$PROJECT_DIR"
source .venv/bin/activate

exec 9>"$LOCK_FILE"

if ! flock -n 9; then
  echo "$(date -u) another monitor run is active; skipping" >> "$LOG_DIR/monitor.log"
  exit 0
fi

echo "" >> "$LOG_DIR/monitor.log"
echo "================================================================================" >> "$LOG_DIR/monitor.log"
echo "$(date -u) starting production monitor" >> "$LOG_DIR/monitor.log"

echo "Checking algorithm integrity..." >> "$LOG_DIR/monitor.log"

if ! python tools/algorithm_integrity.py check >> "$LOG_DIR/monitor.log" 2>&1; then
  echo "$(date -u) algorithm integrity failed; running heal.py" >> "$LOG_DIR/monitor.log"
  python heal.py --verify full >> "$LOG_DIR/monitor.log" 2>&1 || true
fi

echo "Running docker-local production gate..." >> "$LOG_DIR/monitor.log"

if python prod_check.py --level full --environment docker_local --stress-requests-per-route 20 >> "$LOG_DIR/monitor.log" 2>&1; then
  echo "$(date -u) production gate passed" >> "$LOG_DIR/monitor.log"
  exit 0
fi

echo "$(date -u) production gate failed; running self-healing" >> "$LOG_DIR/monitor.log"

python heal.py --verify full >> "$LOG_DIR/monitor.log" 2>&1 || true

echo "$(date -u) re-running production gate after healing" >> "$LOG_DIR/monitor.log"

if python prod_check.py --level full --environment docker_local --stress-requests-per-route 20 >> "$LOG_DIR/monitor.log" 2>&1; then
  echo "$(date -u) healed successfully" >> "$LOG_DIR/monitor.log"
  exit 0
fi

echo "$(date -u) healing failed; manual investigation required" >> "$LOG_DIR/monitor.log"
exit 1
