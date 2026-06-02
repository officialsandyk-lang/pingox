#!/usr/bin/env bash
set -euo pipefail

echo "🔎 Secret scanning"

if command -v gitleaks >/dev/null 2>&1; then
  gitleaks detect --source . --redact
  echo "✅ gitleaks scan passed"
else
  echo "⚠️ gitleaks not installed; skipping"
  echo "Install: brew install gitleaks"
fi

echo "Checking forbidden tracked secret files..."

if git ls-files | grep -E '(^\.env$|\.key$|\.pem$|\.p12$|\.pfx$|data/tls/)'; then
  echo "❌ Secret/cert/key files are tracked by Git"
  exit 1
fi

echo "✅ No forbidden secret files tracked"
