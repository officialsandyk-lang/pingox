from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
PROJECT_DIR = PROJECT_ROOT / "generated-pingora-proxy"
CONFIG_PATH = PROJECT_DIR / "config.json"

os.chdir(PROJECT_ROOT)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from core.production_readiness import (
    apply_production_readiness_profile,
    format_production_readiness_report,
    validate_production_readiness,
)


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def sync_config(config: dict) -> None:
    write_json(PROJECT_DIR / "config.json", config)
    write_json(PROJECT_ROOT / "active_config.json", config)
    write_json(PROJECT_ROOT / "gateway_config.json", config)
    write_json(PROJECT_ROOT / "generated-projects" / "default-project" / "current_config.json", config)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check local production-readiness profile")

    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply production-readiness profile to generated-pingora-proxy/config.json",
    )

    parser.add_argument(
        "--environment",
        default="local",
        choices=["local", "production"],
        help="Environment profile to validate.",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    config = json.load(open(CONFIG_PATH))

    if args.apply:
        config = apply_production_readiness_profile(config, environment=args.environment)

        from core.project_writer import write_project

        write_project(config, project_dir=PROJECT_DIR)
        sync_config(config)

        print("✅ production-readiness profile applied and project regenerated")

    result = validate_production_readiness(
        config,
        environment=args.environment,
        source="prod_check",
        store=True,
    )

    print(format_production_readiness_report(result))

    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
