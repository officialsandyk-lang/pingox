from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent
PROJECT_DIR = PROJECT_ROOT / "generated-pingora-proxy"
BACKUP_DIR = PROJECT_ROOT / "data" / "update_backups"

os.chdir(PROJECT_ROOT)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd or PROJECT_ROOT),
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def port_from_config(config: dict[str, Any]) -> int:
    try:
        return int(config.get("port") or config.get("listen_port") or config.get("proxy_port") or 8090)
    except Exception:
        return 8090


def live_url_for_config(config: dict[str, Any]) -> str:
    return f"http://127.0.0.1:{port_from_config(config)}/"


def list_local_backups() -> list[Path]:
    if not BACKUP_DIR.exists():
        return []

    backups = [
        item
        for item in BACKUP_DIR.iterdir()
        if item.is_dir()
        and item.name.startswith("generated-pingora-proxy-")
        and (item / "config.json").exists()
    ]

    return sorted(backups, key=lambda p: p.stat().st_mtime, reverse=True)


def copy_project_tree(src: Path, dst: Path) -> None:
    if not src.exists():
        raise FileNotFoundError(f"Missing source project backup: {src}")

    if dst.exists():
        shutil.rmtree(dst)

    ignore = shutil.ignore_patterns(
        "target",
        ".runtime",
        "tmp",
        "*.log",
        "__pycache__",
        ".pytest_cache",
    )

    shutil.copytree(src, dst, ignore=ignore)


def save_current_project_snapshot() -> Path | None:
    if not PROJECT_DIR.exists():
        return None

    rollback_snapshot_dir = BACKUP_DIR / f"rollback-current-before-restore-{utc_stamp()}"
    rollback_snapshot_dir.parent.mkdir(parents=True, exist_ok=True)

    ignore = shutil.ignore_patterns(
        "target",
        ".runtime",
        "tmp",
        "*.log",
        "__pycache__",
        ".pytest_cache",
    )

    shutil.copytree(PROJECT_DIR, rollback_snapshot_dir, ignore=ignore)

    return rollback_snapshot_dir


def sync_config_mirrors(config: dict[str, Any]) -> None:
    write_json(PROJECT_DIR / "config.json", config)
    write_json(PROJECT_ROOT / "active_config.json", config)
    write_json(PROJECT_ROOT / "gateway_config.json", config)
    write_json(PROJECT_ROOT / "generated-projects" / "default-project" / "current_config.json", config)


def regenerate_project_from_config(config: dict[str, Any]) -> None:
    from core.project_writer import write_project

    write_project(config, project_dir=PROJECT_DIR)


def kill_port(port: int) -> None:
    run_command(
        [
            "bash",
            "-lc",
            f"lsof -tiTCP:{int(port)} -sTCP:LISTEN | xargs -r kill -9",
        ],
        cwd=PROJECT_ROOT,
        timeout=20,
    )


def start_demo_backends(config_path: Path, restart: bool = False) -> None:
    script = PROJECT_ROOT / "scripts" / "demo_backends.py"

    if not script.exists():
        print("ℹ️ scripts/demo_backends.py not found; skipping demo backend start.")
        return

    command = "restart" if restart else "start"

    result = run_command(
        [sys.executable, str(script), command, str(config_path)],
        cwd=PROJECT_ROOT,
        timeout=120,
    )

    if result.stdout.strip():
        print(result.stdout.strip())

    if result.returncode != 0:
        raise RuntimeError(
            "Failed to start demo backends\n\n"
            f"STDOUT:\n{result.stdout}\n\n"
            f"STDERR:\n{result.stderr}"
        )


def start_local_gateway(config: dict[str, Any]) -> dict[str, Any]:
    port = port_from_config(config)

    runtime_dir = PROJECT_DIR / ".runtime"
    log_dir = PROJECT_DIR / "tmp" / "logs"

    runtime_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / f"rollback-local-pingora-{port}.log"

    print("")
    print("🚀 Restarting local Pingora gateway after rollback...")
    print(f"Live URL: http://127.0.0.1:{port}/")
    print(f"Log file: {log_file}")

    kill_port(port)

    with log_file.open("a", encoding="utf-8", errors="replace") as log:
        process = subprocess.Popen(
            ["cargo", "run"],
            cwd=str(PROJECT_DIR),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            text=True,
            start_new_session=True,
        )

    (runtime_dir / f"rollback-local-pingora-{port}.pid").write_text(str(process.pid), encoding="utf-8")

    deadline = time.time() + 600

    while time.time() < deadline:
        if process.poll() is not None:
            tail = log_file.read_text(encoding="utf-8", errors="replace")[-5000:]
            raise RuntimeError(f"Local gateway exited before ready.\n\n{tail}")

        result = run_command(
            [
                "bash",
                "-lc",
                f"curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{port}/",
            ],
            cwd=PROJECT_ROOT,
            timeout=10,
        )

        status = result.stdout.strip()

        if status in {"200", "301", "302", "403", "404", "405"}:
            print(f"✅ Local gateway ready: HTTP {status}")
            return {
                "success": True,
                "pid": process.pid,
                "live_url": f"http://127.0.0.1:{port}/",
                "status": status,
                "log_file": str(log_file),
            }

        time.sleep(1)

    tail = log_file.read_text(encoding="utf-8", errors="replace")[-5000:] if log_file.exists() else ""
    print(f"⚠️ Local gateway did not become ready within timeout on port {port}.")
    print("The process may still be compiling. Check the log below:")
    print(tail)
    raise RuntimeError(f"Local gateway did not become ready on port {port}.\\n\\n{tail}")


def verify_after_rollback(config: dict[str, Any], level: str) -> dict[str, Any]:
    if level == "none":
        return {
            "success": True,
            "skipped": True,
            "level": "none",
        }

    from core.post_change_verifier import format_verification_report, verify_after_change

    result = verify_after_change(
        config=config,
        config_path=PROJECT_DIR / "config.json",
        live_url=live_url_for_config(config),
        source="rollback",
        change_type="local_direct_rollback",
        level=level,
        store=True,
    )

    print("")
    print(format_verification_report(result))

    if not result.get("success"):
        raise RuntimeError(
            "Post-rollback verification failed: "
            + str(result.get("failure_summary"))
        )

    return result


def rollback_local_direct(
    *,
    backup_path: Path | None = None,
    restart_local: bool = True,
    restart_backends: bool = True,
    verify: str = "smoke",
) -> dict[str, Any]:
    backups = list_local_backups()

    if backup_path is None:
        if not backups:
            raise RuntimeError(
                f"No local update backups found in {BACKUP_DIR}. "
                "Expected folders like data/update_backups/generated-pingora-proxy-YYYYMMDDTHHMMSSZ"
            )

        backup_path = backups[0]

    backup_path = backup_path.resolve()

    if not backup_path.exists():
        raise FileNotFoundError(f"Backup path does not exist: {backup_path}")

    if not (backup_path / "config.json").exists():
        raise FileNotFoundError(f"Backup is missing config.json: {backup_path}")

    print("")
    print("↩️ Local direct rollback")
    print(f"Restoring backup: {backup_path}")

    current_snapshot = save_current_project_snapshot()

    if current_snapshot:
        print(f"Safety snapshot of current project: {current_snapshot}")

    copy_project_tree(backup_path, PROJECT_DIR)

    config = read_json(PROJECT_DIR / "config.json")

    sync_config_mirrors(config)

    # Important: regenerate homepage and Rust build_routes() from restored config.
    regenerate_project_from_config(config)
    sync_config_mirrors(config)

    if restart_backends:
        start_demo_backends(PROJECT_DIR / "config.json", restart=True)

    gateway_result = None

    if restart_local:
        gateway_result = start_local_gateway(config)

    verification = verify_after_rollback(config, verify) if restart_local else {
        "success": True,
        "skipped": True,
        "reason": "local gateway restart disabled",
    }

    return {
        "success": True,
        "mode": "local_direct",
        "restored_backup": str(backup_path),
        "current_snapshot": str(current_snapshot) if current_snapshot else None,
        "active_color": "local",
        "old_active_color": "local",
        "live_url": live_url_for_config(config),
        "gateway": gateway_result,
        "verification": verification,
        "route_count": len(config.get("routes") or []),
    }


def rollback_bluegreen() -> dict[str, Any]:
    from core.rollback import rollback_to_previous

    result = rollback_to_previous()

    if not isinstance(result, dict):
        result = {
            "success": True,
            "result": result,
        }

    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rollback AI Pingora Gateway",
    )

    parser.add_argument(
        "--mode",
        choices=["local", "bluegreen", "auto"],
        default="auto",
        help="Rollback mode. auto prefers local if local backups exist.",
    )

    parser.add_argument(
        "--backup",
        default=None,
        help="Specific local backup folder to restore.",
    )

    parser.add_argument(
        "--verify",
        choices=["none", "smoke", "algorithm", "crud", "stress", "full"],
        default="smoke",
        help="Post-rollback verification level.",
    )

    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Restore files only; do not restart local gateway.",
    )

    parser.add_argument(
        "--no-backends",
        action="store_true",
        help="Do not restart demo backends.",
    )

    parser.add_argument(
        "--list",
        action="store_true",
        help="List available local direct backups and exit.",
    )

    return parser.parse_args()



def rollback_bluegreen_current(backup_path: str | None = None, verify: str = "full") -> dict:
    """
    Current Docker blue/green rollback.

    The old rollback path used the legacy traffic switcher and checked port 8088
    plus old routes like /users. The current pingora-ai Docker blue/green stack
    runs through main.py --config-file on port 8090, so rollback should restore
    a saved generated-pingora-proxy backup config and redeploy it through the
    same current environment deployer path.
    """
    import json
    import shutil
    import subprocess
    import sys
    from datetime import datetime, UTC
    from pathlib import Path

    project_root = Path(__file__).resolve().parent
    backups_dir = project_root / "data" / "update_backups"

    if backup_path:
        selected_backup = Path(backup_path)
        if not selected_backup.is_absolute():
            selected_backup = project_root / selected_backup
    else:
        candidates = [
            item
            for item in backups_dir.glob("generated-pingora-proxy-*")
            if item.is_dir()
        ]

        if not candidates:
            raise RuntimeError(
                f"No generated-pingora-proxy backups found in {backups_dir}"
            )

        selected_backup = max(candidates, key=lambda item: item.stat().st_mtime)

    config_file = selected_backup / "config.json"

    if not config_file.exists():
        nested = selected_backup / "generated-pingora-proxy" / "config.json"
        if nested.exists():
            config_file = nested

    if not config_file.exists():
        raise RuntimeError(f"Backup config not found: {selected_backup}")

    current_project = project_root / "generated-pingora-proxy"

    safety_dir = backups_dir / (
        "rollback-current-before-bluegreen-"
        + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    )

    if current_project.exists():
        safety_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(current_project, safety_dir, dirs_exist_ok=True)
        print(f"Safety snapshot of current project: {safety_dir}")

    print("")
    print("↩️ Docker blue/green rollback")
    print(f"Restoring backup config: {config_file}")

    command = [
        sys.executable,
        "main.py",
        "--runtime",
        "docker_host",
        "--strategy",
        "bluegreen",
        "--verify",
        verify,
        "--config-file",
        str(config_file),
        "__rollback_config_file_deploy__",
    ]

    print("")
    print("Running current rollback deploy:")
    print(" ".join(command))

    result = subprocess.run(
        command,
        cwd=str(project_root),
        text=True,
        capture_output=True,
        timeout=1800,
    )

    if result.stdout:
        print(result.stdout)

    if result.stderr:
        print(result.stderr)

    if result.returncode != 0:
        raise RuntimeError(
            "Docker blue/green rollback deploy failed.\n"
            f"Return code: {result.returncode}\n"
            f"STDOUT tail:\n{result.stdout[-4000:]}\n"
            f"STDERR tail:\n{result.stderr[-4000:]}"
        )

    return {
        "success": True,
        "mode": "bluegreen",
        "strategy": "bluegreen",
        "runtime": "docker_host",
        "backup": str(selected_backup),
        "config_file": str(config_file),
        "verify": verify,
        "live_url": "http://127.0.0.1:8090/",
    }

def main() -> int:
    args = parse_args()

    backups = list_local_backups()

    if args.list:
        print("")
        print("Available local direct backups:")

        if not backups:
            print(f"None found in {BACKUP_DIR}")
            return 0

        for index, backup in enumerate(backups, start=1):
            print(f"{index}. {backup}")

        return 0

    mode = args.mode

    if mode == "auto":
        mode = "local" if backups else "bluegreen"

    if mode == "local":
        result = rollback_local_direct(
            backup_path=Path(args.backup) if args.backup else None,
            restart_local=not args.no_restart,
            restart_backends=not args.no_backends,
            verify=args.verify,
        )
    else:
        result = rollback_bluegreen_current(args.backup, args.verify)

    print("")
    print("✅ Rolled back successfully")
    print(f"Mode: {result.get('mode', mode)}")
    print(f"Active color: {result.get('active_color')}")
    print(f"Previous active color: {result.get('old_active_color')}")
    print(f"Live URL: {result.get('live_url')}")
    print(f"Routes: {result.get('route_count', 'unknown')}")

    verification = result.get("verification")

    if isinstance(verification, dict):
        print(f"Verification: {verification.get('level')} success={verification.get('success')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
