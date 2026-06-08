#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "🛑 Stopping AI Pingora local/Docker environment"
echo "Project: $PROJECT_ROOT"
echo ""

kill_port() {
  local port="$1"
  local pids=""

  pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"

  if [[ -n "$pids" ]]; then
    echo "Killing process(es) on port $port: $pids"
    kill -9 $pids 2>/dev/null || true
  fi
}

docker_available() {
  docker info >/dev/null 2>&1
}

echo "1) Stopping FastAPI server on 8000..."
pkill -f "uvicorn server.main:app" 2>/dev/null || true
kill_port 8000

echo ""
echo "2) Stopping local Pingora gateway on 8090..."
kill_port 8090

echo ""
echo "3) Stopping demo backends..."

if [[ -f "scripts/demo_backends.py" && -f "generated-pingora-proxy/config.json" ]]; then
  python scripts/demo_backends.py stop generated-pingora-proxy/config.json 2>/dev/null || true
fi

python - <<'PY' | while read -r port; do
import json
import re
from pathlib import Path

root = Path(".")
paths = [
    root / "generated-pingora-proxy" / "config.json",
    root / "data" / "api_plans" / "latest_config.json",
]

ports = set()

def add_address(value):
    text = str(value or "")
    match = re.search(r":(\d{2,5})\b", text)
    if match:
        ports.add(int(match.group(1)))

for path in paths:
    if not path.exists():
        continue

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        continue

    for route in data.get("routes", []):
        if not isinstance(route, dict):
            continue

        add_address(route.get("upstream"))
        add_address(route.get("backend"))

        for item in route.get("upstreams", []) or []:
            if isinstance(item, dict):
                add_address(item.get("address") or item.get("upstream") or item.get("backend"))
            else:
                add_address(item)

# Common demo ports used during tests.
for port in [9101, 9102, 9103, 9201, 9202, 9203, 9301, 9302, 9401, 9402, 9499, 9501, 9502, 9503]:
    ports.add(port)

for port in sorted(ports):
    print(port)
PY
  kill_port "$port"
done

echo ""
echo "4) Stopping Docker blue/green stack..."

if docker_available; then
  docker compose -f docker-bluegreen/docker-compose.yml down --remove-orphans 2>/dev/null || true

  docker rm -f \
    ai-pingora-gateway-blue \
    ai-pingora-gateway-green \
    ai-pingora-gateway-edge \
    pingora-edge-router \
    2>/dev/null || true

  docker ps -aq --filter "name=ai-pingora-gateway" | xargs -r docker rm -f 2>/dev/null || true
  docker ps -aq --filter "name=pingora" | xargs -r docker rm -f 2>/dev/null || true

  docker network rm docker-bluegreen_ai_pingora_net 2>/dev/null || true
  docker network rm pingora-edge-net 2>/dev/null || true
else
  echo "⚠️ Docker daemon not available. Skipping Docker cleanup."
fi

echo ""
echo "5) Final status"

if docker_available; then
  docker ps -a --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | grep -E "ai-pingora|pingora|docker-bluegreen" || echo "✅ No Pingora Docker containers"
else
  echo "Docker unavailable, cannot list containers."
fi

echo ""
echo "Port checks:"
for port in 8000 8090 9101 9102 9103 9401 9402 9499; do
  if lsof -tiTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "⚠️ Port $port still has a listener"
  else
    echo "✅ Port $port free"
  fi
done

echo ""
echo "✅ Environment stop completed"
