from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


PROJECT_DIR = Path("generated-pingora-proxy")
DEFAULT_BASE_CONFIG = PROJECT_DIR / "config.json"


ENVIRONMENT_PROFILES: dict[str, dict[str, Any]] = {
    "local": {
        "filename": "config.local.json",
        "runtime": "local",
        "runtime_mode": "local",
        "strategy": "direct",
        "deployment_strategy": "direct",
        "address_mode": "local",
        "port": 8080,
        "listen_port": 8080,
        "live_url": "http://127.0.0.1:8080/",
        "replace_from": ["host.docker.internal:", "localhost:"],
        "replace_to": "127.0.0.1:",
    },
    "docker": {
        "filename": "config.docker.json",
        "runtime": "docker_host",
        "runtime_mode": "docker_host",
        "strategy": "bluegreen",
        "deployment_strategy": "bluegreen",
        "address_mode": "docker_host",
        "port": 80,
        "listen_port": 80,
        "https_port": 443,
        "live_url": "http://127.0.0.1:80/",
        "replace_from": ["127.0.0.1:", "localhost:"],
        "replace_to": "host.docker.internal:",
    },
    "docker_host": {
        "alias_for": "docker",
    },
    "linux": {
        "filename": "config.linux.json",
        "runtime": "linux_systemd",
        "runtime_mode": "linux_systemd",
        "strategy": "bluegreen",
        "deployment_strategy": "bluegreen",
        "address_mode": "linux_systemd",
        "port": 80,
        "listen_port": 80,
        "https_port": 443,
        "live_url": "http://<host>:80/",
        "replace_from": ["host.docker.internal:"],
        "replace_to": "127.0.0.1:",
    },
    "linux_systemd": {
        "alias_for": "linux",
    },
}


def normalise_env_name(env: str | None) -> str:
    value = str(env or "local").strip().lower().replace("-", "_")

    profile = ENVIRONMENT_PROFILES.get(value)
    if isinstance(profile, dict) and profile.get("alias_for"):
        return str(profile["alias_for"])

    return value


def register_environment_profile(name: str, profile: dict[str, Any]) -> None:
    """
    Future-safe extension point.

    Example:
      register_environment_profile("kubernetes", {...})
      -> generated-pingora-proxy/config.kubernetes.json
    """
    key = normalise_env_name(name)
    ENVIRONMENT_PROFILES[key] = dict(profile)


def env_config_path(env: str, project_dir: str | Path = PROJECT_DIR) -> Path:
    env_name = normalise_env_name(env)
    profile = ENVIRONMENT_PROFILES.get(env_name, {})

    filename = profile.get("filename") or f"config.{env_name}.json"
    return Path(project_dir) / str(filename)


def _rewrite_address(address: str, profile: dict[str, Any]) -> str:
    value = str(address or "").strip()

    replace_to = str(profile.get("replace_to") or "")
    for prefix in profile.get("replace_from") or []:
        value = value.replace(str(prefix), replace_to)

    return value


def _rewrite_route_addresses(route: dict[str, Any], profile: dict[str, Any]) -> None:
    upstreams = route.get("upstreams")

    if isinstance(upstreams, list):
        rewritten = []

        for item in upstreams:
            if isinstance(item, dict):
                updated = copy.deepcopy(item)
                for key in ("address", "upstream", "backend"):
                    if updated.get(key):
                        updated[key] = _rewrite_address(str(updated[key]), profile)
                rewritten.append(updated)
            else:
                rewritten.append(_rewrite_address(str(item), profile))

        route["upstreams"] = rewritten

    for key in ("upstream", "backend"):
        if route.get(key):
            route[key] = _rewrite_address(str(route[key]), profile)


def build_env_config(base_config: dict[str, Any], env: str) -> dict[str, Any]:
    env_name = normalise_env_name(env)
    profile = ENVIRONMENT_PROFILES.get(env_name)

    if not isinstance(profile, dict):
        # Generic future environment fallback.
        profile = {
            "runtime": env_name,
            "runtime_mode": env_name,
            "strategy": "direct",
            "deployment_strategy": "direct",
            "address_mode": env_name,
            "port": base_config.get("port") or 80,
            "listen_port": base_config.get("listen_port") or base_config.get("port") or 80,
            "live_url": base_config.get("live_url") or f"http://<{env_name}>/",
            "replace_from": [],
            "replace_to": "",
        }

    cfg = copy.deepcopy(base_config)

    # Environment configs must not inherit stale decision evidence from another runtime.
    # The active environment guard should regenerate/record fresh decisions per env.
    cfg.pop("_system_decision_result", None)
    cfg.pop("_decision_result", None)
    cfg.pop("decision_result", None)

    for key in (
        "runtime",
        "runtime_mode",
        "strategy",
        "deployment_strategy",
        "address_mode",
        "port",
        "listen_port",
        "https_port",
        "live_url",
    ):
        if key in profile:
            cfg[key] = profile[key]

    cfg["environment"] = env_name
    cfg["env_config_name"] = env_name
    cfg["env_config_file"] = str(env_config_path(env_name))

    routes = cfg.get("routes") or []
    if isinstance(routes, list):
        for route in routes:
            if isinstance(route, dict):
                _rewrite_route_addresses(route, profile)

    return cfg


def write_env_config(
    env: str,
    base_config: dict[str, Any],
    project_dir: str | Path = PROJECT_DIR,
) -> Path:
    path = env_config_path(env, project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    cfg = build_env_config(base_config, env)
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return path


def sync_env_configs(
    base_config_path: str | Path = DEFAULT_BASE_CONFIG,
    project_dir: str | Path = PROJECT_DIR,
    environments: list[str] | None = None,
) -> dict[str, str]:
    base_path = Path(base_config_path)
    if not base_path.exists():
        raise FileNotFoundError(f"Missing base config: {base_path}")

    base = json.loads(base_path.read_text(encoding="utf-8"))

    envs = environments or ["local", "docker", "linux"]
    written: dict[str, str] = {}

    for env in envs:
        env_name = normalise_env_name(env)
        path = write_env_config(env_name, base, project_dir)
        written[env_name] = str(path)

    return written


def load_env_config(
    env: str,
    project_dir: str | Path = PROJECT_DIR,
    fallback_base_config: str | Path = DEFAULT_BASE_CONFIG,
) -> dict[str, Any]:
    path = env_config_path(env, project_dir)

    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))

    base_path = Path(fallback_base_config)
    if not base_path.exists():
        raise FileNotFoundError(f"Missing config for env={env}: {path}")

    base = json.loads(base_path.read_text(encoding="utf-8"))
    cfg = build_env_config(base, env)

    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg


def main() -> int:
    written = sync_env_configs()
    for env, path in written.items():
        print(f"✅ {env}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
