from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent
PROJECT_DIR = PROJECT_ROOT / "generated-pingora-proxy"
STAGING_DIR = PROJECT_ROOT / "tmp" / "update-staged-pingora-proxy"
BACKUP_DIR = PROJECT_ROOT / "data" / "update_backups"


try:
    from ai.ai_config import prompt_to_config
except Exception:
    from ai_config import prompt_to_config


from agents.config_repair_agent import repair_config
from agents.security_agent import enforce_security
from core.validator import validate_config
from core.project_writer import write_project

try:
    from agents.runtime_agent import run_cargo_check, run_local_gateway
except Exception:
    run_cargo_check = None
    run_local_gateway = None


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def utc_now_slug() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}")

    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def normalize_path(path: Any) -> str:
    text = str(path or "/").strip()

    if not text.startswith("/"):
        text = "/" + text

    text = re.sub(r"[^a-zA-Z0-9/_\-.]", "", text)

    if not text:
        return "/"

    if len(text) > 1:
        text = text.rstrip("/")

    return text


def prompt_mentions_port(prompt: str) -> bool:
    return bool(re.search(r"\bport\s+\d+\b", prompt, flags=re.IGNORECASE))


def prompt_mentions_security(prompt: str) -> bool:
    return bool(
        re.search(
            r"\b(rate\s+limit|max\s+connections|max\s+request|allowed\s+methods|only\s+allow|block|blocked|timeout)\b",
            prompt,
            flags=re.IGNORECASE,
        )
    )


def route_key(route: dict[str, Any]) -> str:
    return normalize_path(
        route.get("path")
        or route.get("prefix")
        or route.get("route")
        or "/"
    )


def is_static_route(route: dict[str, Any]) -> bool:
    return str(route.get("type") or "").strip().lower() in {
        "static",
        "web",
        "webserver",
        "web_server",
        "file_server",
        "files",
    }


def merge_routes(
    existing_routes: list[Any],
    update_routes: list[Any],
) -> list[dict[str, Any]]:
    ordered_paths: list[str] = []
    by_path: dict[str, dict[str, Any]] = {}

    for route in existing_routes:
        if not isinstance(route, dict):
            continue

        path = route_key(route)

        if path not in by_path:
            ordered_paths.append(path)

        by_path[path] = copy.deepcopy(route)

    for route in update_routes:
        if not isinstance(route, dict):
            continue

        path = route_key(route)

        if path not in by_path:
            ordered_paths.append(path)

        # Update prompt wins for that route path.
        by_path[path] = copy.deepcopy(route)
        by_path[path]["path"] = path

    return [by_path[path] for path in ordered_paths]


def merge_config_update(
    existing_config: dict[str, Any],
    update_config: dict[str, Any],
    prompt: str,
) -> dict[str, Any]:
    """
    Additive update:
    - keeps existing routes
    - replaces only routes mentioned in update prompt/config
    - preserves existing port unless prompt explicitly mentions a port
    - preserves security unless prompt includes security changes
    """

    merged = copy.deepcopy(existing_config or {})

    if not merged:
        merged = {
            "port": 8090,
            "routes": [],
        }

    if prompt_mentions_port(prompt):
        if update_config.get("port"):
            merged["port"] = update_config.get("port")
        elif update_config.get("listen_port"):
            merged["port"] = update_config.get("listen_port")
        elif update_config.get("proxy_port"):
            merged["port"] = update_config.get("proxy_port")

    existing_routes = merged.get("routes") or []
    update_routes = update_config.get("routes") or []

    if not isinstance(existing_routes, list):
        existing_routes = []

    if not isinstance(update_routes, list):
        update_routes = []

    merged["routes"] = merge_routes(existing_routes, update_routes)

    if prompt_mentions_security(prompt):
        security = copy.deepcopy(merged.get("security") or {})
        update_security = update_config.get("security") or {}

        if isinstance(update_security, dict):
            security.update(update_security)

        merged["security"] = security

    for key in (
        "runtime",
        "runtime_mode",
        "strategy",
        "deployment_strategy",
        "demo_backend",
    ):
        if key in existing_config and key not in merged:
            merged[key] = existing_config[key]

    return merged


def apply_runtime_addressing_local(config: dict[str, Any], runtime_mode: str) -> dict[str, Any]:
    """
    Minimal runtime addressing.

    local:
      keep 127.0.0.1 as-is

    docker_host:
      rewrite 127.0.0.1/localhost upstreams to host.docker.internal
    """

    fixed = copy.deepcopy(config)
    fixed["runtime_mode"] = runtime_mode

    if runtime_mode == "local":
        return fixed

    def rewrite_address(value: Any) -> Any:
        if isinstance(value, dict):
            item = copy.deepcopy(value)

            for key in ("address", "upstream", "backend", "target", "url"):
                if key in item and item[key]:
                    item[key] = rewrite_address(item[key])

            return item

        text = str(value or "").strip()
        text = text.replace("http://", "").replace("https://", "").rstrip("/")

        if text.startswith("127.0.0.1:"):
            return "host.docker.internal:" + text.split(":", 1)[1]

        if text.startswith("localhost:"):
            return "host.docker.internal:" + text.split(":", 1)[1]

        if text.startswith("0.0.0.0:"):
            return "host.docker.internal:" + text.split(":", 1)[1]

        return text

    for route in fixed.get("routes", []) or []:
        if not isinstance(route, dict):
            continue

        if is_static_route(route):
            continue

        for key in ("upstream", "backend", "target"):
            if key in route:
                route[key] = rewrite_address(route[key])

        for key in ("upstreams", "backends", "backend_upstreams"):
            if isinstance(route.get(key), list):
                route[key] = [rewrite_address(item) for item in route[key]]

    return fixed


def run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def cargo_check(project_dir: Path) -> bool:
    if run_cargo_check is not None:
        result = run_cargo_check(project_dir, attempts=3)

        if isinstance(result, dict):
            return bool(result.get("success") or result.get("ok"))

        return bool(result)

    result = run_command(["cargo", "check"], cwd=project_dir, timeout=240)

    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)

    return result.returncode == 0


def kill_gateway_port(port: int) -> None:
    run_command(
        [
            "bash",
            "-lc",
            f"lsof -tiTCP:{int(port)} -sTCP:LISTEN | xargs -r kill -9",
        ],
        cwd=PROJECT_ROOT,
        timeout=20,
    )


def start_demo_backends(config_path: Path) -> None:
    script = PROJECT_ROOT / "scripts" / "demo_backends.py"

    if not script.exists():
        print("⚠️ scripts/demo_backends.py not found, skipping backend start")
        return

    result = run_command(
        [sys.executable, str(script), "start", str(config_path)],
        cwd=PROJECT_ROOT,
        timeout=60,
    )

    if result.stdout.strip():
        print(result.stdout.strip())

    if result.returncode != 0:
        print(result.stderr.strip())
        raise RuntimeError("Failed to start demo backends")


def start_gateway(project_dir: Path, port: int) -> None:
    if run_local_gateway is not None:
        result = run_local_gateway(
            project_dir=project_dir,
            port=port,
            startup_timeout_seconds=90,
            stop_existing=False,
        )

        if isinstance(result, dict):
            ok = bool(result.get("success") or result.get("ok") or result.get("runtime_ok"))

            if not ok:
                raise RuntimeError(result.get("error") or result.get("message") or result)

            return

        if result:
            return

    process = subprocess.Popen(
        ["cargo", "run"],
        cwd=str(project_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        text=True,
    )

    runtime_dir = project_dir / ".runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / f"local-pingora-{port}.pid").write_text(str(process.pid), encoding="utf-8")

    deadline = datetime.now().timestamp() + 90

    while datetime.now().timestamp() < deadline:
        result = run_command(
            ["bash", "-lc", f"curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{port}/"],
            timeout=5,
        )

        if result.stdout.strip() in {"200", "301", "302", "403", "404", "405"}:
            return

    raise RuntimeError("Gateway did not become ready after update")


def backup_current_project() -> Path | None:
    if not PROJECT_DIR.exists():
        return None

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    backup_path = BACKUP_DIR / f"generated-pingora-proxy-{utc_now_slug()}"

    if backup_path.exists():
        shutil.rmtree(backup_path)

    ignore = shutil.ignore_patterns(
        "target",
        ".runtime",
        "tmp",
        "*.log",
    )

    shutil.copytree(PROJECT_DIR, backup_path, ignore=ignore)

    return backup_path


def copy_project_tree(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)

    for item in src.iterdir():
        if item.name in {"target", ".runtime"}:
            continue

        target = dst / item.name

        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()

        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)


def smoke_test(config: dict[str, Any]) -> bool:
    port = int(config.get("port") or 8090)
    routes = config.get("routes") or []

    if not isinstance(routes, list):
        return False

    ok = True

    for route in routes:
        if not isinstance(route, dict):
            continue

        path = normalize_path(route.get("path") or "/")
        url_path = path if path.endswith("/") else path + "/"
        url = f"http://127.0.0.1:{port}{url_path}"

        result = run_command(
            [
                "bash",
                "-lc",
                f"curl -s -o /tmp/update_smoke_body.txt -w '%{{http_code}}' {url}",
            ],
            cwd=PROJECT_ROOT,
            timeout=10,
        )

        status = result.stdout.strip()

        route_ok = status in {"200", "301", "302", "403", "404", "405"}

        # Dynamic proxy routes should not be 502.
        if not is_static_route(route):
            route_ok = status == "200"

        print(f"{'✅' if route_ok else '❌'} {path} -> HTTP {status}")

        if not route_ok:
            ok = False

    return ok


# ---------------------------------------------------------------------
# Main update flow
# ---------------------------------------------------------------------


def update_gateway(
    *,
    prompt: str,
    runtime: str = "local",
    strategy: str = "direct",
    restart: bool = True,
) -> dict[str, Any]:
    config_path = PROJECT_DIR / "config.json"

    if not config_path.exists():
        raise FileNotFoundError(
            f"{config_path} does not exist. Create the gateway first with main.py."
        )

    print("📖 Loading current live config...")
    current_config = load_json(config_path)

    print("🧠 Parsing update prompt...")
    update_config = prompt_to_config(prompt)

    print("AI understood update as:")
    print(json.dumps(update_config, indent=2))

    print("🔀 Merging update into existing config...")
    merged_config = merge_config_update(
        current_config,
        update_config,
        prompt,
    )

    merged_config["runtime"] = runtime
    merged_config["runtime_mode"] = runtime
    merged_config["strategy"] = strategy
    merged_config["deployment_strategy"] = strategy

    print("🛠️ Repairing merged config...")
    repaired_config = repair_config(
        merged_config,
        prompt=prompt,
    )

    repaired_config = enforce_security(
        repaired_config,
        prompt=prompt,
    )

    repaired_config = apply_runtime_addressing_local(
        repaired_config,
        runtime_mode=runtime,
    )

    print("✅ Validating merged config...")
    final_config = validate_config(repaired_config)

    final_config["runtime"] = runtime
    final_config["runtime_mode"] = runtime
    final_config["strategy"] = strategy
    final_config["deployment_strategy"] = strategy

    print("Final updated config:")
    print(json.dumps(final_config, indent=2))

    print("🧪 Writing staged project while live URL remains online...")
    if STAGING_DIR.exists():
        shutil.rmtree(STAGING_DIR)

    write_project(final_config, project_dir=STAGING_DIR)

    print("🔍 Running cargo check on staged project...")
    if not cargo_check(STAGING_DIR):
        raise RuntimeError(
            "Staged update failed cargo check. Live gateway was not touched."
        )

    print("✅ Staged cargo check passed. Existing live gateway has not been stopped.")

    backup_path = backup_current_project()
    if backup_path:
        print(f"💾 Backup created: {backup_path}")

    print("📦 Promoting staged project to live project folder...")
    copy_project_tree(STAGING_DIR, PROJECT_DIR)

    write_json(PROJECT_DIR / "config.json", final_config)

    print("🚀 Starting any new demo backend ports...")
    start_demo_backends(PROJECT_DIR / "config.json")

    port = int(final_config.get("port") or 8090)

    if restart:
        print("🔁 Restarting gateway at final step only...")
        print("⚠️ Same-port local update has a brief restart window.")
        kill_gateway_port(port)
        start_gateway(PROJECT_DIR, port)
    else:
        print("ℹ️ Restart skipped. Source/config updated, but running gateway still has old routes.")

    print("🧪 Smoke testing updated live URL...")
    smoke_ok = smoke_test(final_config)

    if not smoke_ok:
        raise RuntimeError("Update promoted, but smoke test failed.")

    print("")
    print("✅ Update completed successfully")
    print(f"Live URL: http://127.0.0.1:{port}/")

    return {
        "success": True,
        "live_url": f"http://127.0.0.1:{port}/",
        "config": final_config,
        "backup": str(backup_path) if backup_path else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Safely add/update AI Pingora Gateway routes while keeping the old gateway live until final restart."
    )

    parser.add_argument("prompt", nargs="*", help="Natural-language update prompt")
    parser.add_argument("--runtime", default="local")
    parser.add_argument("--strategy", default="direct")
    parser.add_argument("--no-restart", action="store_true")

    args = parser.parse_args()

    prompt = " ".join(args.prompt).strip()

    if not prompt:
        print("Usage:")
        print('  python update.py "add /search across 127.0.0.1:9601 and 127.0.0.1:9602 using random"')
        return 2

    try:
        update_gateway(
            prompt=prompt,
            runtime=args.runtime,
            strategy=args.strategy,
            restart=not args.no_restart,
        )
        return 0
    except Exception as exc:
        print("")
        print("❌ Update failed.")
        print(exc)
        print("")
        print("Existing gateway should still be safe unless failure happened after final promotion.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())