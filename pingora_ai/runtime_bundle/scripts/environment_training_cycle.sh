#!/usr/bin/env bash
set -euo pipefail

echo "Training environment reliability knowledge..."
python scripts/train_environment_agents.py

echo "Running Docker guardian..."
python core/stability_guardian.py --environment docker || true

echo "Running Linux guardian..."
python core/stability_guardian.py --environment linux --host "${1:-192.168.252.2}" || true

echo "Latest guardian learning history:"
python - <<'PY'
import sqlite3
from pathlib import Path

conn = sqlite3.connect(Path("data/reliability_reports.sqlite3"))

print("\nGuardian runs:")
for row in conn.execute("""
    SELECT environment, success, created_at
    FROM stability_guardian_runs
    ORDER BY id DESC
    LIMIT 10
"""):
    print(row)

print("\nKnown lessons:")
for row in conn.execute("""
    SELECT pattern_id, root_cause, category
    FROM reliability_knowledge
    ORDER BY created_at_utc DESC
    LIMIT 12
"""):
    print(row)

conn.close()
PY
