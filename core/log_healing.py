from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
UTC = timezone.utc
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_DIR = PROJECT_ROOT / "generated-pingora-proxy"
CONFIG_PATH = PROJECT_DIR / "config.json"
DB_PATH = PROJECT_ROOT / "data" / "reliability_reports.sqlite3"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


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


def tail_file(path: Path, max_chars: int = 12000) -> str:
    if not path.exists():
        return ""

    try:
        return path.read_text(encoding="utf-8", errors="replace")[-max_chars:]
    except Exception as exc:
        return f"Could not read {path}: {exc}"


def collect_log_files(config: dict[str, Any]) -> list[Path]:
    port = port_from_config(config)

    candidates = [
        PROJECT_DIR / "tmp" / "logs" / f"local-pingora-{port}.log",
        PROJECT_DIR / "tmp" / "logs" / f"rollback-local-pingora-{port}.log",
        PROJECT_DIR / "tmp" / "logs" / f"reset-local-pingora-{port}.log",
        PROJECT_ROOT / "logs.jsonl",
    ]

    log_dir = PROJECT_DIR / "tmp" / "logs"

    if log_dir.exists():
        candidates.extend(sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:10])

    unique: list[Path] = []
    seen: set[str] = set()

    for item in candidates:
        key = str(item.resolve())

        if key not in seen:
            seen.add(key)
            unique.append(item)

    return unique


def collect_system_evidence(
    *,
    config: dict[str, Any],
    verify_level: str,
) -> dict[str, Any]:
    from core.post_change_verifier import verify_after_change

    logs = []

    for path in collect_log_files(config):
        logs.append(
            {
                "path": str(path),
                "exists": path.exists(),
                "tail": tail_file(path),
            }
        )

    live_url = live_url_for_config(config)

    try:
        verification = verify_after_change(
            config=config,
            config_path=CONFIG_PATH,
            live_url=live_url,
            source="log_healing_precheck",
            change_type="system_log_detection",
            level=verify_level,
            store=True,
        )
    except Exception as exc:
        verification = {
            "success": False,
            "error": str(exc),
            "exception_type": type(exc).__name__,
        }

    port = port_from_config(config)

    port_check = run_command(
        ["bash", "-lc", f"lsof -iTCP:{port} -sTCP:LISTEN || true"],
        timeout=20,
    )

    curl_check = run_command(
        ["bash", "-lc", f"curl -s -o /tmp/pingora-heal-homepage.txt -w '%{{http_code}}' {live_url} || true"],
        timeout=20,
    )

    homepage_sample = ""

    sample_path = Path("/tmp/pingora-heal-homepage.txt")

    if sample_path.exists():
        homepage_sample = tail_file(sample_path, max_chars=4000)

    return {
        "created_at": utc_now(),
        "live_url": live_url,
        "port": port,
        "port_check_stdout": port_check.stdout,
        "port_check_stderr": port_check.stderr,
        "curl_status": curl_check.stdout.strip(),
        "homepage_sample": homepage_sample,
        "verification": verification,
        "logs": logs,
    }


def ensure_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS log_healing_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                success INTEGER NOT NULL,
                root_cause TEXT,
                repair_actions_json TEXT,
                attempts_json TEXT,
                final_verification_json TEXT,
                evidence_json TEXT
            )
            """
        )
        conn.commit()


def store_healing_run(
    *,
    success: bool,
    root_cause: str | None,
    repair_actions: list[str],
    attempts: list[dict[str, Any]],
    final_verification: dict[str, Any] | None,
    evidence: dict[str, Any],
) -> None:
    ensure_db()

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO log_healing_runs (
                created_at,
                success,
                root_cause,
                repair_actions_json,
                attempts_json,
                final_verification_json,
                evidence_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(),
                1 if success else 0,
                root_cause,
                json.dumps(repair_actions, indent=2, default=str),
                json.dumps(attempts, indent=2, default=str),
                json.dumps(final_verification, indent=2, default=str),
                json.dumps(evidence, indent=2, default=str),
            ),
        )
        conn.commit()


def create_backup(reason: str = "log_healing_before_repair") -> Path | None:
    try:
        from core.local_backup import create_project_backup

        return create_project_backup(PROJECT_DIR, reason=reason)
    except Exception:
        backup_root = PROJECT_ROOT / "data" / "update_backups"
        backup_root.mkdir(parents=True, exist_ok=True)

        if not PROJECT_DIR.exists():
            return None

        destination = backup_root / f"generated-pingora-proxy-{utc_stamp()}-log-healing"

        ignore = shutil.ignore_patterns(
            "target",
            ".runtime",
            "tmp",
            "*.log",
            "__pycache__",
            ".pytest_cache",
        )

        shutil.copytree(PROJECT_DIR, destination, ignore=ignore)

        return destination


def sync_config_mirrors(config: dict[str, Any]) -> None:
    write_json(PROJECT_DIR / "config.json", config)
    write_json(PROJECT_ROOT / "active_config.json", config)
    write_json(PROJECT_ROOT / "gateway_config.json", config)
    write_json(PROJECT_ROOT / "generated-projects" / "default-project" / "current_config.json", config)


def apply_repaired_config(config: dict[str, Any]) -> None:
    from core.project_writer import write_project

    write_project(config, project_dir=PROJECT_DIR)
    sync_config_mirrors(config)


def kill_port(port: int) -> None:
    run_command(
        [
            "bash",
            "-lc",
            f"lsof -tiTCP:{int(port)} -sTCP:LISTEN | xargs -r kill -9",
        ],
        timeout=30,
    )


def start_demo_backends(config_path: Path) -> None:
    script = PROJECT_ROOT / "scripts" / "demo_backends.py"

    if not script.exists():
        print("ℹ️ scripts/demo_backends.py not found; skipping demo backend restart.")
        return

    result = run_command(
        [sys.executable, str(script), "restart", str(config_path)],
        cwd=PROJECT_ROOT,
        timeout=180,
    )

    if result.stdout.strip():
        print(result.stdout.strip())

    if result.returncode != 0:
        raise RuntimeError(
            "Failed to restart demo backends\n\n"
            f"STDOUT:\n{result.stdout}\n\n"
            f"STDERR:\n{result.stderr}"
        )


def start_local_gateway(config: dict[str, Any]) -> dict[str, Any]:
    port = port_from_config(config)
    live_url = live_url_for_config(config)

    runtime_dir = PROJECT_DIR / ".runtime"
    log_dir = PROJECT_DIR / "tmp" / "logs"

    runtime_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / f"log-healing-local-pingora-{port}.log"

    print("")
    print("🚀 Starting local gateway after healing...")
    print(f"Live URL: {live_url}")
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

    (runtime_dir / f"log-healing-local-pingora-{port}.pid").write_text(str(process.pid), encoding="utf-8")

    deadline = time.time() + 600

    while time.time() < deadline:
        if process.poll() is not None:
            tail = tail_file(log_file)
            raise RuntimeError(f"Local gateway exited before ready.\n\n{tail}")

        result = run_command(
            [
                "bash",
                "-lc",
                f"curl -s -o /dev/null -w '%{{http_code}}' {live_url}",
            ],
            timeout=10,
        )

        status = result.stdout.strip()

        if status in {"200", "301", "302", "403", "404", "405"}:
            print(f"✅ Local gateway ready: HTTP {status}")
            return {
                "success": True,
                "pid": process.pid,
                "live_url": live_url,
                "status": status,
                "log_file": str(log_file),
            }

        time.sleep(1)

    raise RuntimeError(f"Local gateway did not become ready on port {port}.\n\n{tail_file(log_file)}")


def run_final_verification(config: dict[str, Any], level: str) -> dict[str, Any]:
    from core.post_change_verifier import format_verification_report, verify_after_change

    result = verify_after_change(
        config=config,
        config_path=CONFIG_PATH,
        live_url=live_url_for_config(config),
        source="log_healing",
        change_type="auto_heal_after_system_log_detection",
        level=level,
        store=True,
    )

    print("")
    print(format_verification_report(result))

    return result


def heal_from_system_logs(
    *,
    verify_level: str = "full",
    auto_repair: bool = True,
    restart: bool = True,
    max_attempts: int = 3,
) -> dict[str, Any]:
    from agents.root_cause_agent import (
        diagnose_and_repair_config,
        update_learning_repair_result,
    )

    attempts: list[dict[str, Any]] = []
    all_repair_actions: list[str] = []
    last_root_cause: str | None = None
    last_evidence: dict[str, Any] = {}
    final_verification: dict[str, Any] | None = None

    for attempt_number in range(1, max_attempts + 1):
        print("")
        print("=" * 80)
        print(f"🩺 Log-healing attempt {attempt_number}/{max_attempts}")
        print("=" * 80)

        config = read_json(CONFIG_PATH)
        evidence = collect_system_evidence(config=config, verify_level=verify_level)
        last_evidence = evidence

        verification = evidence.get("verification") or {}

        if verification.get("success"):
            print("✅ System already healthy. No healing needed.")

            result = {
                "success": True,
                "healed": False,
                "reason": "already_healthy",
                "verification": verification,
                "attempts": attempts,
            }

            store_healing_run(
                success=True,
                root_cause=None,
                repair_actions=all_repair_actions,
                attempts=attempts,
                final_verification=verification,
                evidence=evidence,
            )

            return result

        diagnosis = diagnose_and_repair_config(
            config,
            evidence=evidence,
            auto_repair=auto_repair,
            store=True,
        )

        root_cause = diagnosis.get("root_cause")
        last_root_cause = root_cause
        repaired_config = diagnosis.get("repaired_config") or config
        repair_actions = diagnosis.get("repair_actions") or []
        changed = bool(diagnosis.get("changed"))

        all_repair_actions.extend(repair_actions)

        print(diagnosis.get("report") or "")
        print("changed:", changed)
        print("repair_actions:", repair_actions)

        backup_path = None

        if changed:
            backup_path = create_backup(reason=f"log_healing_before_{root_cause}")
            print(f"💾 Backup created: {backup_path}")
            apply_repaired_config(repaired_config)
        elif root_cause in {
            "gateway_not_running_or_not_ready",
            "ip_hash_not_sticky_or_stale_runtime",
            "rust_build_routes_not_regenerated",
        }:
            print("ℹ️ No config change required; will restart/regenerate runtime.")
            apply_repaired_config(repaired_config)
        elif not diagnosis.get("classification", {}).get("is_repairable"):
            print("❌ Failure is not currently repairable by known rules.")

            attempts.append(
                {
                    "attempt": attempt_number,
                    "root_cause": root_cause,
                    "changed": changed,
                    "repair_actions": repair_actions,
                    "backup": str(backup_path) if backup_path else None,
                    "success": False,
                    "reason": "not_repairable",
                }
            )

            break

        if restart:
            start_demo_backends(CONFIG_PATH)
            start_local_gateway(repaired_config)

        final_config = read_json(CONFIG_PATH)
        final_verification = run_final_verification(final_config, verify_level)
        success = bool(final_verification.get("success"))

        try:
            update_learning_repair_result(
                root_cause or "unknown_failure",
                evidence=evidence,
                success=success,
                repair_action=",".join(repair_actions),
            )
        except Exception:
            pass

        attempts.append(
            {
                "attempt": attempt_number,
                "root_cause": root_cause,
                "changed": changed,
                "repair_actions": repair_actions,
                "backup": str(backup_path) if backup_path else None,
                "success": success,
                "verification": final_verification,
            }
        )

        if success:
            store_healing_run(
                success=True,
                root_cause=root_cause,
                repair_actions=all_repair_actions,
                attempts=attempts,
                final_verification=final_verification,
                evidence=evidence,
            )

            return {
                "success": True,
                "healed": True,
                "root_cause": root_cause,
                "repair_actions": all_repair_actions,
                "attempts": attempts,
                "verification": final_verification,
            }

        print("⚠️ Healing attempt did not pass verification; trying again if attempts remain.")

    store_healing_run(
        success=False,
        root_cause=last_root_cause,
        repair_actions=all_repair_actions,
        attempts=attempts,
        final_verification=final_verification,
        evidence=last_evidence,
    )

    return {
        "success": False,
        "healed": False,
        "root_cause": last_root_cause,
        "repair_actions": all_repair_actions,
        "attempts": attempts,
        "verification": final_verification,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Detect and heal local AI Pingora Gateway failures from logs + verifier evidence.",
    )

    parser.add_argument(
        "--verify",
        choices=["smoke", "algorithm", "crud", "stress", "full"],
        default="full",
        help="Verification level used before and after healing.",
    )

    parser.add_argument(
        "--no-repair",
        action="store_true",
        help="Only diagnose; do not repair config.",
    )

    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Repair files only; do not restart local runtime.",
    )

    parser.add_argument(
        "--attempts",
        type=int,
        default=3,
        help="Maximum healing attempts.",
    )

    parser.add_argument(
        "--watch",
        action="store_true",
        help="Keep watching and healing periodically.",
    )

    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Watch interval in seconds.",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.watch:
        print("👀 Starting log-healing watch mode.")

        while True:
            result = heal_from_system_logs(
                verify_level=args.verify,
                auto_repair=not args.no_repair,
                restart=not args.no_restart,
                max_attempts=args.attempts,
            )

            print("")
            print("Watch result:", json.dumps(result, indent=2, default=str)[:4000])
            time.sleep(args.interval)

    result = heal_from_system_logs(
        verify_level=args.verify,
        auto_repair=not args.no_repair,
        restart=not args.no_restart,
        max_attempts=args.attempts,
    )

    print("")
    print("🧾 Final healing result")
    print(json.dumps(result, indent=2, default=str)[:6000])

    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
