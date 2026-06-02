#!/usr/bin/env bash
set -euo pipefail

mkdir -p data/tls

CERT="data/tls/local.crt"
KEY="data/tls/local.key"

if [ -f "$CERT" ] && [ -f "$KEY" ]; then
  echo "✅ Local TLS cert already exists"
  echo "$CERT"
  echo "$KEY"
  exit 0
fi

openssl req -x509 -nodes -newkey rsa:2048 \
  -keyout "$KEY" \
  -out "$CERT" \
  -days 365 \
  -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"

chmod 600 "$KEY"

echo "✅ Generated local self-signed TLS cert"
echo "$CERT"
echo "$KEY"
