"""Production CLI: propose -> review -> validate -> generate. Never auto-deploy."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from core.production import ProductionConfig, generate
from workers.ai_worker import run_worker, json_object


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="Write a proposed configuration for review")
    plan.add_argument("prompt")
    plan.add_argument("--output", type=Path, required=True)
    for command in ("validate", "generate"):
        sub = commands.add_parser(command)
        sub.add_argument("--config", type=Path, required=True)
        if command == "generate":
            sub.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "plan":
            instructions = ("Translate the request into JSON matching this schema. "
                            "Use only requested upstreams. Do not invent destinations. "
                            "Return JSON only.\n" + json.dumps(ProductionConfig.model_json_schema()))
            config = ProductionConfig.model_validate(json_object(run_worker(instructions, args.prompt)))
            # Exclusive creation prevents an AI proposal overwriting a reviewed release.
            with args.output.open("x") as output:
                output.write(config.model_dump_json(indent=2) + "\n")
            print(f"Proposal saved for review: {args.output}")
            return
        config = ProductionConfig.model_validate_json(args.config.read_text())
        if args.command == "generate":
            output = generate(config, args.output)
            print(f"Release generated: {output}")
            print("Build and test the release image using the included Cargo.lock. See docs/PRODUCTION.md.")
        else:
            print("Configuration is valid")
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
