#!/usr/bin/env bash
# CI-only ephemeral Docker test. Never run against a production Compose project.
set -euo pipefail
release_dir=$(mktemp -d)
project_name="pingora-test-$$"
backend_network="${project_name}-backends"
cleanup() {
  docker compose -p "$project_name" --project-directory "$release_dir" -f "$release_dir/compose.yaml" -f "$release_dir/test.yaml" down -v --remove-orphans || true
  docker network rm "$backend_network" >/dev/null 2>&1 || true
  rm -rf "$release_dir"
}
trap cleanup EXIT
python gateway.py generate --config deploy/gateway.example.json --output "$release_dir"
export GATEWAY_IMAGE="pingora-test:$$"
export CADDY_IMAGE="caddy:2"
export GATEWAY_DOMAIN="localhost"
export ACME_EMAIL="ci@example.com"
export UPSTREAM_NETWORK="$backend_network"
export GATEWAY_NETWORK_CIDR="172.30.50.0/24"
docker network create "$backend_network"
cat > "$release_dir/test.yaml" <<'YAML'
services:
  gateway:
    ports: ["127.0.0.1:18088:8080"]
  edge:
    profiles: [production]
  api-backend:
    image: python:3.12-slim
    networks: [upstreams]
    command: ["sh", "-c", "mkdir -p /tmp/api && echo backend-ok > /tmp/api/index.html && cd /tmp && exec python -m http.server 8000"]
YAML
compose=(docker compose -p "$project_name" --project-directory "$release_dir" -f "$release_dir/compose.yaml" -f "$release_dir/test.yaml")
"${compose[@]}" config --quiet
"${compose[@]}" up -d --build --wait --wait-timeout 180 gateway api-backend
curl --fail --retry 10 --retry-all-errors --retry-delay 1 http://127.0.0.1:18088/api/ | grep backend-ok
test "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18088/api/internal)" = 403
test "$("${compose[@]}" exec -T gateway id -u)" = 65532
"${compose[@]}" restart gateway
for attempt in $(seq 1 30); do
  if curl --fail --silent http://127.0.0.1:18088/_gateway/health; then break; fi
  sleep 1
done
curl --fail http://127.0.0.1:18088/api/ | grep backend-ok
# Validate the actual edge configuration, without requesting public certificates.
"${compose[@]}" run --rm --no-deps edge caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
printf 'PASS: Linux container build, routing, policy, non-root runtime, restart, Caddy validation\n'
