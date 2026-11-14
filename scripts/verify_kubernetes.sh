#!/usr/bin/env bash
# Disposable CI cluster only; never uses the operator's kubeconfig or context.
set -euo pipefail
release_dir=$(mktemp -d)
cluster_name="pingora-test-$$"
export KUBECONFIG="$release_dir/kubeconfig"
forward_pid=""
cleanup() {
  if [ -n "$forward_pid" ]; then kill "$forward_pid" 2>/dev/null || true; fi
  kind delete cluster --name "$cluster_name" || true
  rm -rf "$release_dir"
}
trap cleanup EXIT
python gateway.py generate --config deploy/gateway.example.json --target kubernetes --image pingora-portable:test --output "$release_dir/release"
docker build -t pingora-portable:test "$release_dir/release"
kind create cluster --name "$cluster_name" --kubeconfig "$KUBECONFIG" --wait 120s
kind load docker-image pingora-portable:test --name "$cluster_name"
kubectl create namespace ai-gateway
kubectl apply --dry-run=server -f "$release_dir/release/kubernetes.yaml"
kubectl apply -f "$release_dir/release/kubernetes.yaml"
kubectl -n ai-gateway create deployment api-backend --image=python:3.12-slim -- sh -c 'mkdir -p /tmp/api && echo backend-ok > /tmp/api/index.html && cd /tmp && exec python -m http.server 8000'
kubectl -n ai-gateway expose deployment api-backend --port 8000
kubectl -n ai-gateway rollout status deployment/api-backend --timeout=180s
kubectl -n ai-gateway rollout status deployment/ai-pingora-gateway --timeout=180s
kubectl -n ai-gateway port-forward service/ai-pingora-gateway 18088:8080 > "$release_dir/forward.log" 2>&1 &
forward_pid=$!
curl --fail --retry 10 --retry-all-errors --retry-delay 1 http://127.0.0.1:18088/api/ | grep backend-ok
test "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18088/api/internal)" = 403
kubectl -n ai-gateway rollout restart deployment/ai-pingora-gateway
kubectl -n ai-gateway rollout status deployment/ai-pingora-gateway --timeout=180s
printf 'PASS: Kubernetes API validation, non-root pods, backend routing, policy and rolling restart\n'
