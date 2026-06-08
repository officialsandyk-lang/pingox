#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/mnt/c/Users/offic/dev/pingora-generator"
COMPOSE_FILE="$PROJECT_DIR/docker-bluegreen/docker-compose.yml"

echo "🛑 Stopping Docker blue/green gateway stack..."

if [ -f "$COMPOSE_FILE" ]; then
  docker compose -f "$COMPOSE_FILE" down --remove-orphans || true
fi

docker rm -f \
  ai-pingora-gateway-edge \
  ai-pingora-gateway-blue \
  ai-pingora-gateway-green \
  2>/dev/null || true

echo "✅ Docker blue/green gateway stack stopped"
