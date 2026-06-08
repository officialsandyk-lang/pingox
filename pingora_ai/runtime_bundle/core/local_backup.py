from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
UTC = timezone.utc
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROJECT_DIR = PROJECT_ROOT / "generated-pingora-proxy"
DEFAULT_BACKUP_DIR = PROJECT_ROOT / "data" / "update_backups"


def utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def create_project_backup(
    project_dir: str | Path = DEFAULT_PROJECT_DIR,
    backup_dir: str | Path = DEFAULT_BACKUP_DIR,
    *,
    reason: str = "update",
) -> Path:
    project_path = Path(project_dir)
    backup_root = Path(backup_dir)

    if not project_path.exists():
        raise FileNotFoundError(f"Cannot backup missing project directory: {project_path}")

    if not (project_path / "config.json").exists():
        raise FileNotFoundError(f"Cannot backup project without config.json: {project_path}")

    backup_root.mkdir(parents=True, exist_ok=True)

    destination = backup_root / f"{project_path.name}-{utc_stamp()}"

    ignore = shutil.ignore_patterns(
        "target",
        ".runtime",
        "tmp",
        "*.log",
        "__pycache__",
        ".pytest_cache",
    )

    shutil.copytree(project_path, destination, ignore=ignore)

    metadata = {
        "created_at": datetime.now(UTC).isoformat(),
        "reason": reason,
        "source": str(project_path.resolve()),
        "backup": str(destination.resolve()),
    }

    (destination / "backup_metadata.json").write_text(
        json.dumps(metadata, indent=2),
        encoding="utf-8",
    )

    return destination


def list_project_backups(
    backup_dir: str | Path = DEFAULT_BACKUP_DIR,
) -> list[Path]:
    backup_root = Path(backup_dir)

    if not backup_root.exists():
        return []

    backups = [
        item
        for item in backup_root.iterdir()
        if item.is_dir()
        and item.name.startswith("generated-pingora-proxy-")
        and (item / "config.json").exists()
    ]

    return sorted(backups, key=lambda item: item.stat().st_mtime, reverse=True)
