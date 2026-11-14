"""Pingox: propose, validate, build and run a Pingora-powered gateway."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from pydantic import ValidationError
from core.production import ProductionConfig, generate
from core.deployment_targets import DeploymentOptions, TARGETS
from workers.ai_worker import run_worker, json_object


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="Write a proposed configuration for review")
    plan.add_argument("prompt")
    plan.add_argument("--output", type=Path, required=True)
    init = commands.add_parser("init", help="Create an editable project configuration")
    init.add_argument("--directory", type=Path, required=True)
    init.add_argument("--target", choices=TARGETS, default="compose")
    init.add_argument("--image")
    doctor = commands.add_parser("doctor", help="Check local prerequisites without changing services")
    doctor.add_argument("--target", choices=TARGETS, default="compose")
    for command in ("validate", "generate"):
        sub = commands.add_parser(command)
        sub.add_argument("--config", type=Path, required=True)
        if command == "generate":
            sub.add_argument("--output", type=Path, required=True)
            sub.add_argument("--deployment", type=Path, help="Deployment profile JSON")
            sub.add_argument("--target", choices=TARGETS)
            sub.add_argument("--image")
    build = commands.add_parser("build", help="Build a native gateway release (requires Rust)")
    build.add_argument("--config", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    run = commands.add_parser("run", help="Run a built native release in the foreground")
    run.add_argument("--release", type=Path, required=True)
    run.add_argument("--bind", default="127.0.0.1", help="IPv4 listener address; defaults to loopback")
    run.add_argument("--state-dir", type=Path, help="Watch this directory's active.json at runtime")
    for name in ("apply", "rollback"):
        sub = commands.add_parser(name, help="Publish or roll back local runtime configuration")
        sub.add_argument("--state-dir", type=Path, required=True)
        sub.add_argument("--expect-revision", type=int, required=True)
        if name == "apply":
            sub.add_argument("--config", type=Path, required=True)
    status = commands.add_parser("status", help="Read local runtime configuration status")
    status.add_argument("--port", type=int, default=8080)
    bundle = commands.add_parser("package", help="Sign and package a native release")
    bundle.add_argument("--release", type=Path, required=True)
    bundle.add_argument("--output", type=Path, required=True)
    bundle.add_argument("--signing-key", type=Path, required=True)
    bundle.add_argument("--version", required=True)
    for name in ("install", "rollback-release"):
        sub = commands.add_parser(name, help="Verify and select a native release; service restart is separate")
        sub.add_argument("--root", type=Path, required=True)
        sub.add_argument("--public-key", type=Path, required=True)
        if name == "install":
            sub.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "package":
            from core.release_bundle import package
            package(args.release, args.output, args.signing_key, args.version)
            print(f"Signed bundle created: {args.output}")
            return
        if args.command in {"install", "rollback-release"}:
            from core.release_bundle import install, rollback
            version = (install(args.bundle, args.root, args.public_key) if args.command == "install"
                       else rollback(args.root, args.public_key))
            print(f"Verified release {version} selected. Restart your managed service and verify health.")
            return
        if args.command == "status":
            import http.client
            if not 1024 <= args.port <= 65535:
                raise ValueError("invalid port")
            conn = http.client.HTTPConnection("127.0.0.1", args.port, timeout=3)
            try:
                conn.request("GET", "/_gateway/config")
                response = conn.getresponse()
                if response.status != 200:
                    raise RuntimeError("gateway status unavailable")
                print(json.dumps(json.loads(response.read(4096)), indent=2))
            finally:
                conn.close()
            return
        if args.command in {"apply", "rollback"}:
            from core.config_store import publish, read_config
            config = read_config(args.config) if args.command == "apply" else None
            result = publish(args.state_dir, config, args.expect_revision)
            print(f"Published revision {result.revision}. Use 'pingox.py status --port {result.port}' to verify runtime acceptance.")
            return
        if args.command in {"build", "run"}:
            from core.local_service import build_release, run_release
            if args.command == "build":
                output = build_release(args.config, args.output)
                print(f"Native release built: {output}. Run with: python pingox.py run --release {output}")
            else:
                run_release(args.release, args.bind, args.state_dir)
            return
        if args.command == "doctor":
            import shutil
            requirements = {"compose": ["docker"], "container": ["docker"],
                            "kubernetes": ["docker", "kubectl"], "systemd": ["cargo", "cc", "cmake"]}
            missing = [name for name in requirements[args.target] if not shutil.which(name)]
            if missing:
                parser.exit(1, "Missing local tools: " + ", ".join(missing) + "\n")
            print("Local executables found. This does not validate remote credentials, daemon/cluster access, TLS or backends.")
            return
        if args.command == "init":
            import shutil
            options = DeploymentOptions(target=args.target, image=args.image)
            if args.directory.exists() and any(args.directory.iterdir()):
                raise ValueError("directory must be empty")
            args.directory.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Path(__file__).parent / "deploy/gateway.example.json", args.directory / "gateway.json")
            (args.directory / "deployment.json").write_text(options.model_dump_json(indent=2) + "\n")
            print(f"Edit {args.directory}/gateway.json and deployment.json, then validate and generate.")
            return
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
            if args.deployment and (args.target or args.image):
                raise ValueError("use a deployment profile OR target/image flags")
            options = (DeploymentOptions.model_validate_json(args.deployment.read_text()) if args.deployment
                       else DeploymentOptions(target=args.target or "compose", image=args.image))
            output = generate(config, args.output, options)
            print(f"Release generated: {output}")
            print("Build and test the release image using the included Cargo.lock. See docs/PRODUCTION.md.")
        else:
            print("Configuration is valid")
    except ValidationError as exc:
        details = "\n".join(".".join(str(v) for v in e["loc"]) + ": " + e["msg"]
                            for e in exc.errors(include_input=False, include_url=False))
        parser.exit(1, "Configuration invalid:\n" + details + "\n")
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
