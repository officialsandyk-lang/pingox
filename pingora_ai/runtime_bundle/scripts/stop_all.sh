#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "🛑 Stopping AI Pingora full environment..."
echo "Project: $PROJECT_ROOT"
echo ""

kill_port() {
  local port="$1"
  local pids=""

  pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"

  if [[ -n "$pids" ]]; then
    echo "Killing port $port: $pids"
    kill -9 $pids 2>/dev/null || true
  else
    echo "Port $port already free"
  fi
}

echo "1) Stopping FastAPI..."
pkill -f "uvicorn server.main:app" 2>/dev/null || true
kill_port 8000

echo ""
echo "2) Stopping local Pingora gateway..."
pkill -f "generated-pingora-proxy" 2>/dev/null || true
kill_port 8090

echo ""
echo "3) Stopping demo backend ports..."
for port in 9101 9102 9103 9201 9202 9203 9301 9302 9401 9402 9499 9501 9502 9503; do
  kill_port "$port"
done

echo ""
echo "4) Connecting to Docker Desktop..."

if ./scripts/docker_ready.sh 120; then
  echo ""
  echo "5) Stopping Docker blue/green stack..."

  docker --context desktop-linux compose \
    -f docker-bluegreen/docker-compose.yml \
    down --remove-orphans 2>/dev/null || true

  docker --context desktop-linux rm -f \
    ai-pingora-gateway-edge \
    ai-pingora-gateway-blue \
    ai-pingora-gateway-green \
    pingora-edge-router \
    pingora-proxy-blue \
    pingora-proxy-green \
    2>/dev/null || true

  docker --context desktop-linux network rm \
    docker-bluegreen_ai_pingora_net \
    ai_pingora_net \
    pingora-blue_default \
    pingora-green_default \
    pingora-edge-net \
    2>/dev/null || true

  docker --context desktop-linux container prune -f >/dev/null 2>&1 || true

  echo "✅ Docker cleanup complete"
else
  echo "⚠️ Docker cleanup skipped because Docker Desktop did not become ready."
fi

echo ""
echo "6) Final status"

if docker --context desktop-linux info >/dev/null 2>&1; then
  docker --context desktop-linux ps -a --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" \
    | grep -E "ai-pingora|pingora|docker-bluegreen" \
    || echo "✅ No Pingora Docker containers"
else
  echo "Docker unavailable, cannot list containers."
fi

echo ""
echo "Port checks:"
for port in 8000 8090 9101 9102 9103 9401 9402 9499; do
  if lsof -tiTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "⚠️ Port $port still running"
  else
    echo "✅ Port $port free"
  fi
done

echo ""
echo "✅ Environment stop completed"
