#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

echo "🛑 Stopping AI Pingora Docker blue/green stack..."

# Current Docker blue/green stack
if [ -f "docker-bluegreen/docker-compose.yml" ]; then
  docker compose \
    -f docker-bluegreen/docker-compose.yml \
    down --remove-orphans 2>/dev/null || true
fi

# Remove current known containers directly, in case compose state is broken
docker rm -f \
  ai-pingora-gateway-edge \
  ai-pingora-gateway-blue \
  ai-pingora-gateway-green \
  2>/dev/null || true

# Legacy/older blue-green stack cleanup
if [ -f "generated-projects/default-project/edge-router/docker-compose.yml" ]; then
  docker compose \
    -f generated-projects/default-project/edge-router/docker-compose.yml \
    down --remove-orphans 2>/dev/null || true
fi

if [ -f "generated-projects/default-project/blue/generated-pingora-proxy/docker-compose.bluegreen.yml" ]; then
  docker compose \
    -p pingora-blue \
    -f generated-projects/default-project/blue/generated-pingora-proxy/docker-compose.bluegreen.yml \
    down --remove-orphans 2>/dev/null || true
fi

if [ -f "generated-projects/default-project/green/generated-pingora-proxy/docker-compose.bluegreen.yml" ]; then
  docker compose \
    -p pingora-green \
    -f generated-projects/default-project/green/generated-pingora-proxy/docker-compose.bluegreen.yml \
    down --remove-orphans 2>/dev/null || true
fi

# Legacy container names
docker rm -f \
  pingora-edge-router \
  pingora-proxy-blue \
  pingora-proxy-green \
  2>/dev/null || true

# Optional cleanup of unused networks created by old runs
docker network rm \
  docker-bluegreen_ai_pingora_net \
  ai_pingora_net \
  pingora-blue_default \
  pingora-green_default \
  2>/dev/null || true

echo ""
echo "✅ Stopped Pingora Docker blue/green stack"
echo ""
echo "Current Docker containers:"
docker ps