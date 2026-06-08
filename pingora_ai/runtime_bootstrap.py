from __future__ import annotations

import os
import shutil
from pathlib import Path
from importlib import resources


def user_home() -> Path:
    return Path(os.environ.get("PINGORA_AI_HOME", "~/.pingora-ai")).expanduser()


def runtime_root() -> Path:
    env_root = os.environ.get("PINGORA_AI_PROJECT_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    return user_home() / "runtime"


def is_runtime_ready(root: Path) -> bool:
    return (
        (root / "main.py").exists()
        and (root / "update.py").exists()
        and (root / "core").exists()
        and (root / "templates" / "pingora_runtime" / "src" / "main.rs").exists()
    )


def bootstrap_runtime(force: bool = False) -> Path:
    target = runtime_root()

    if os.environ.get("PINGORA_AI_PROJECT_ROOT"):
        if not is_runtime_ready(target):
            raise RuntimeError(f"PINGORA_AI_PROJECT_ROOT is set but runtime is incomplete: {target}")
        return target

    if force and target.exists():
        shutil.rmtree(target)

    if is_runtime_ready(target):
        return target

    src = resources.files("pingora_ai").joinpath("runtime_bundle")

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)

    shutil.copytree(src, target)

    if not is_runtime_ready(target):
        raise RuntimeError("Runtime bootstrap failed. Packaged runtime is incomplete.")

    return target


def ensure_runtime_for_operation() -> Path:
    root = bootstrap_runtime(force=False)
    os.environ["PINGORA_AI_PROJECT_ROOT"] = str(root)

    pythonpath = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = str(root) if not pythonpath else str(root) + os.pathsep + pythonpath

    return root
