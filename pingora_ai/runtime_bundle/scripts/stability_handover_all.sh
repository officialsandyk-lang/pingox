#!/usr/bin/env bash
set -euo pipefail

HOST="${1:-192.168.252.2}"

echo "=============================="
echo "STABILITY GUARDIAN: LOCAL"
echo "=============================="
python core/stability_guardian.py --environment local

echo "=============================="
echo "STABILITY GUARDIAN: DOCKER"
echo "=============================="
python core/stability_guardian.py --environment docker

echo "=============================="
echo "STABILITY GUARDIAN: BARE METAL"
echo "=============================="
python core/stability_guardian.py --environment linux --host "$HOST"

echo "=============================="
echo "ALL STABILITY GUARDIAN CHECKS PASSED"
echo "=============================="
