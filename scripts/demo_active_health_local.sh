#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

TARGET_PORT="${1:-9102}"
TARGET_UPSTREAM="127.0.0.1:${TARGET_PORT}"
STATE_URL="http://127.0.0.1:8080/__gateway_runtime_state"
DASHBOARD_URL="http://127.0.0.1:8080/?runtime_context=local&address_mode=local"

echo "🧪 AI Pingora Gateway Active Health Demo"
echo "Target backend: ${TARGET_UPSTREAM}"
echo "Dashboard: ${DASHBOARD_URL}"
echo

echo "1) Starting local gateway and demo backends..."
pingora-ai start --local

echo
echo "2) Confirming ${TARGET_UPSTREAM} starts healthy..."
python - <<PY
import json, time, urllib.request

state_url = "${STATE_URL}"
target = "${TARGET_UPSTREAM}"

def rows():
    with urllib.request.urlopen(state_url, timeout=10) as r:
        state = json.loads(r.read().decode())
    return [u for u in state["active_health"]["upstreams"] if u["upstream"] == target]

deadline = time.time() + 40
while time.time() < deadline:
    data = rows()
    if data and all(item.get("healthy") is True for item in data):
        print(json.dumps(data, indent=2))
        raise SystemExit(0)
    time.sleep(2)

print(json.dumps(rows(), indent=2))
raise SystemExit("Backend did not become healthy before demo.")
PY

echo
echo "3) Stopping backend ${TARGET_UPSTREAM}..."
lsof -ti :"${TARGET_PORT}" | xargs kill -9 2>/dev/null || true

echo
echo "4) Waiting for active health to mark it unhealthy..."
python - <<PY
import json, time, urllib.request

state_url = "${STATE_URL}"
target = "${TARGET_UPSTREAM}"

def rows():
    with urllib.request.urlopen(state_url, timeout=10) as r:
        state = json.loads(r.read().decode())
    return [u for u in state["active_health"]["upstreams"] if u["upstream"] == target]

deadline = time.time() + 75
while time.time() < deadline:
    data = rows()
    if data and all(item.get("healthy") is False for item in data):
        print(json.dumps(data, indent=2))
        raise SystemExit(0)
    time.sleep(2)

print(json.dumps(rows(), indent=2))
raise SystemExit("Backend did not become unhealthy in time.")
PY

echo
echo "✅ Open dashboard now and check:"
echo "   - Healthy backend targets should drop"
echo "   - Unhealthy backend targets should increase"
echo "   - ${TARGET_UPSTREAM} rows should be red"
echo "   - last_error should show connection failure"
echo
echo "Dashboard: ${DASHBOARD_URL}"
echo
read -r -p "Press ENTER after viewing the unhealthy dashboard state..."

echo
echo "5) Restarting local gateway/demo backends..."
pingora-ai start --local

echo
echo "6) Waiting for active health recovery..."
python - <<PY
import json, time, urllib.request

state_url = "${STATE_URL}"
target = "${TARGET_UPSTREAM}"

def rows():
    with urllib.request.urlopen(state_url, timeout=10) as r:
        state = json.loads(r.read().decode())
    return [u for u in state["active_health"]["upstreams"] if u["upstream"] == target]

deadline = time.time() + 75
while time.time() < deadline:
    data = rows()
    if data and all(item.get("healthy") is True for item in data):
        print(json.dumps(data, indent=2))
        raise SystemExit(0)
    time.sleep(2)

print(json.dumps(rows(), indent=2))
raise SystemExit("Backend did not recover in time.")
PY

echo
echo "✅ Active health demo completed successfully."
echo "Dashboard: ${DASHBOARD_URL}"
