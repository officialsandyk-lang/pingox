#!/usr/bin/env bash
set -Eeuo pipefail

TIMEOUT_SECONDS="${1:-120}"

unset DOCKER_HOST || true
docker context use desktop-linux >/dev/null 2>&1 || true

if docker info >/dev/null 2>&1; then
  echo "✅ Docker connected"
  exit 0
fi

echo "🐳 Docker is not connected. Starting Docker Desktop..."
open -a Docker || true

deadline=$((SECONDS + TIMEOUT_SECONDS))

while [ "$SECONDS" -lt "$deadline" ]; do
  unset DOCKER_HOST || true
  docker context use desktop-linux >/dev/null 2>&1 || true

  if docker info >/dev/null 2>&1; then
    echo "✅ Docker connected"
    exit 0
  fi

  echo "⏳ Waiting for Docker Desktop..."
  sleep 3
done

echo "❌ Docker did not become ready within ${TIMEOUT_SECONDS}s"
echo "Open Docker Desktop manually, wait until it says Docker is running, then retry."
exit 1
