from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from core.algorithm_integrity import (
    DEFAULT_BASELINE_PATH,
    save_algorithm_integrity_baseline,
    validate_algorithm_integrity,
)


DEFAULT_CONFIG = PROJECT_ROOT / "generated-pingora-proxy" / "config.json"


def load_config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Algorithm integrity baseline/check tool.")
    parser.add_argument("command", choices=["write", "check"])
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--baseline", default=str(DEFAULT_BASELINE_PATH))
    parser.add_argument("--source", default="manual_verified_baseline")
    parser.add_argument("--allow-extra-routes", action="store_true")

    args = parser.parse_args()

    config = load_config(Path(args.config))

    if args.command == "write":
        baseline = save_algorithm_integrity_baseline(
            config,
            baseline_path=args.baseline,
            source=args.source,
        )

        print("✅ Algorithm integrity baseline written")
        print(f"Path: {args.baseline}")
        print(f"Hash: {baseline.get('integrity_hash')}")
        print(f"Routes: {len(baseline.get('routes') or {})}")
        return 0

    result = validate_algorithm_integrity(
        config,
        baseline_path=args.baseline,
        require_baseline=True,
        allow_extra_routes=args.allow_extra_routes,
    )

    if result["success"]:
        print("✅ Algorithm integrity check passed")
        print(f"Baseline: {args.baseline}")
        print(f"Hash: {result.get('actual_hash')}")
        return 0

    print("❌ Algorithm integrity check failed")
    print(json.dumps(result, indent=2, default=str)[:12000])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
