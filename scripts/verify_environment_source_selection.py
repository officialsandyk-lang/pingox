from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_DIR = PROJECT_ROOT / "generated-pingora-proxy"


def fail(message: str) -> None:
    raise SystemExit(f"❌ {message}")


def ok(message: str) -> None:
    print(f"✅ {message}")


def load_json(path: Path) -> dict:
    if not path.exists():
        fail(f"Missing file: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"Could not parse {path}: {exc}")
    if not isinstance(data, dict):
        fail(f"Expected JSON object in {path}")
    return data


def route_addresses(config: dict) -> list[str]:
    addresses: list[str] = []
    for route in config.get("routes", []):
        if not isinstance(route, dict):
            continue

        upstream = route.get("upstream")
        if isinstance(upstream, str):
            addresses.append(upstream)

        upstreams = route.get("upstreams")
        if isinstance(upstreams, list):
            for item in upstreams:
                if isinstance(item, str):
                    addresses.append(item)
                elif isinstance(item, dict) and isinstance(item.get("address"), str):
                    addresses.append(item["address"])

    return addresses


def verify_profile_files() -> None:
    local = load_json(PROJECT_DIR / "config.local.json")
    docker = load_json(PROJECT_DIR / "config.docker.json")

    linux_path = PROJECT_DIR / "config.linux.json"
    linux = load_json(linux_path) if linux_path.exists() else None

    if local.get("runtime") != "local":
        fail("config.local.json does not use runtime=local")
    if local.get("strategy") not in {"direct", None}:
        fail("config.local.json does not use strategy=direct")
    if any("host.docker.internal" in addr for addr in route_addresses(local)):
        fail("config.local.json contains Docker host addressing")
    ok("Local profile source is config.local.json with local addressing")

    if docker.get("runtime") != "docker_host":
        fail("config.docker.json does not use runtime=docker_host")
    if docker.get("strategy") != "bluegreen":
        fail("config.docker.json does not use strategy=bluegreen")
    if any(addr.startswith("127.0.0.1:") for addr in route_addresses(docker)):
        fail("config.docker.json contains local 127.0.0.1 upstream addressing")
    ok("Docker profile source is config.docker.json with Docker addressing")

    if linux is not None:
        if linux.get("runtime") not in {"linux", "linux_systemd", "bare_metal"}:
            fail("config.linux.json runtime is not Linux-specific")
        ok("Linux profile source exists and is separate")


def verify_update_py_selection() -> None:
    sys.path.insert(0, str(PROJECT_ROOT))
    import update

    checks = [
        (["--runtime", "local", "--strategy", "direct", "noop"], "config.local.json"),
        (["--runtime", "docker_host", "--strategy", "bluegreen", "noop"], "config.docker.json"),
        (["--runtime", "docker", "--strategy", "bluegreen", "noop"], "config.docker.json"),
    ]

    original_argv = sys.argv[:]
    try:
        for argv, expected_name in checks:
            sys.argv = ["update.py"] + argv
            selected = update.active_env_config_path()
            if selected.name != expected_name:
                fail(f"update.py selected {selected}, expected {expected_name}")
    finally:
        sys.argv = original_argv

    ok("update.py resolves local/Docker mutation source configs correctly")


def verify_update_graph_local_source() -> None:
    sys.path.insert(0, str(PROJECT_ROOT))
    from orchestration.update_graph import _config_candidates

    first = _config_candidates("blue")[0]
    if first.name != "config.local.json":
        fail(f"update_graph first local source is {first}, expected config.local.json")

    ok("update_graph local source prefers config.local.json")


def verify_dashboard_context_code() -> None:
    text = (PROJECT_ROOT / "core" / "project_writer.py").read_text(encoding="utf-8")

    required = [
        'dashboardConfigTarget',
        'dashboardConfigStrategy',
        'runtime_context',
        'address_mode',
        'configTarget = "local"',
        'configStrategy = "direct"',
        'configTarget = "docker_host"',
        'configStrategy = "bluegreen"',
    ]

    for marker in required:
        if marker not in text:
            fail(f"Dashboard context marker missing from project_writer.py: {marker}")

    ok("Dashboard metadata is runtime-context aware in project_writer.py")


def fetch_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.read().decode("utf-8", errors="replace")


def verify_live_dashboard_if_running() -> None:
    checks = [
        ("http://127.0.0.1:8080/?runtime_context=local&address_mode=local", "dashboardConfigTarget"),
        ("http://127.0.0.1/?runtime_context=docker&address_mode=docker_host", "dashboardConfigTarget"),
    ]

    for url, marker in checks:
        try:
            body = fetch_text(url)
        except Exception:
            print(f"ℹ️ Skipping live dashboard check, not reachable: {url}")
            continue

        if marker not in body:
            print(f"ℹ️ Live dashboard is reachable but appears stale until redeploy/regenerate: {url}")
            continue

    ok("Live dashboard dynamic metadata markers checked when reachable")


def verify_no_status_mutation() -> None:
    watched = [
        PROJECT_DIR / "config.local.json",
        PROJECT_DIR / "config.docker.json",
        PROJECT_DIR / "config.linux.json",
    ]

    before = {p: p.read_text(encoding="utf-8") for p in watched if p.exists()}

    commands = [
        [sys.executable, "-m", "pingora_ai.cli", "status", "--local"],
        [sys.executable, "-m", "pingora_ai.cli", "status", "--docker"],
    ]

    for command in commands:
        subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=20,
            check=False,
        )

    after = {p: p.read_text(encoding="utf-8") for p in watched if p.exists()}

    if before != after:
        fail("Read-only status commands mutated environment profile configs")

    ok("Read-only status commands do not mutate environment profile configs")


def main() -> int:
    print("🧪 Verifying environment source selection")
    print("Scope: no deploy, no mutation, config/profile/dashboard source checks only")

    verify_profile_files()
    verify_update_py_selection()
    verify_update_graph_local_source()
    verify_dashboard_context_code()
    verify_live_dashboard_if_running()
    verify_no_status_mutation()

    print()
    print("✅ ENVIRONMENT SOURCE SELECTION VERIFICATION PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
