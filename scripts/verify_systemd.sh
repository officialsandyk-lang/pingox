#!/usr/bin/env bash
# GitHub's disposable Linux runner only, never a production host.
set -euo pipefail
if [ "${GITHUB_ACTIONS:-}" != "true" ]; then
  echo 'This test installs a temporary service and is restricted to a disposable GitHub runner.' >&2
  exit 1
fi
release_dir=$(mktemp -d)
backend_pid=""
cleanup() {
  sudo systemctl stop ai-pingora-gateway.service || true
  sudo rm -f /etc/systemd/system/ai-pingora-gateway.service
  sudo systemctl daemon-reload
  if [ -n "$backend_pid" ]; then kill "$backend_pid" 2>/dev/null || true; fi
  sudo rm -rf /opt/ai-pingora-gateway
  rm -rf "$release_dir"
}
# Refuse to overwrite an existing installation, even on a runner.
test ! -e /opt/ai-pingora-gateway
test ! -e /etc/systemd/system/ai-pingora-gateway.service
trap cleanup EXIT
python - "$release_dir/config.json" <<'PY'
import json, sys
from pathlib import Path
config = json.loads(Path('deploy/gateway.example.json').read_text())
config['port'] = 18088
config['routes'][0]['upstreams'][0]['address'] = '127.0.0.1:18089'
Path(sys.argv[1]).write_text(json.dumps(config))
PY
python gateway.py generate --config "$release_dir/config.json" --target systemd --output "$release_dir/release"
cargo build --locked --manifest-path "$release_dir/release/Cargo.toml"
sudo install -D "$release_dir/release/target/debug/generated-pingora-proxy" /opt/ai-pingora-gateway/current/gateway
sudo install -m 644 "$release_dir/release/ai-pingora-gateway.service" /etc/systemd/system/ai-pingora-gateway.service
sudo systemd-analyze verify /etc/systemd/system/ai-pingora-gateway.service
mkdir -p "$release_dir/backend/api"
echo backend-ok > "$release_dir/backend/api/index.html"
python -m http.server 18089 --bind 127.0.0.1 --directory "$release_dir/backend" > "$release_dir/backend.log" 2>&1 &
backend_pid=$!
sudo systemctl daemon-reload
sudo systemctl start ai-pingora-gateway.service
curl --fail --retry 10 --retry-all-errors --retry-delay 1 http://127.0.0.1:18088/api/ | grep backend-ok
sudo systemctl restart ai-pingora-gateway.service
curl --fail --retry 10 --retry-all-errors --retry-delay 1 http://127.0.0.1:18088/api/ | grep backend-ok
sudo systemctl is-active ai-pingora-gateway.service
printf 'PASS: systemd unit validation, DynamicUser, localhost routing and restart\n'
