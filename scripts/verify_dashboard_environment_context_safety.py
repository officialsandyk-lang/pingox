from __future__ import annotations

import json
import urllib.request


CONTROL = "http://127.0.0.1:8787"
TOKEN = "dev-local-control-token"


def fail(message: str) -> None:
    print(f"❌ {message}")
    raise SystemExit(1)


def ok(message: str) -> None:
    print(f"✅ {message}")


def control(path: str) -> dict:
    req = urllib.request.Request(
        CONTROL + path,
        headers={"X-Pingora-Control-Token": TOKEN},
    )
    with urllib.request.urlopen(req, timeout=10) as res:
        data = json.loads(res.read().decode("utf-8"))
    if not isinstance(data, dict):
        fail(f"Expected JSON object from {path}")
    return data


def assert_blocked_non_local(action: str) -> None:
    result = control(
        f"/__gateway_control/runtime-admin/upstream/{action}"
        f"?route=/api"
        f"&upstream=host.docker.internal:9101"
        f"&confirm=true"
        f"&runtime_context=docker"
        f"&address_mode=docker_host"
    )

    if result.get("ok") is not False:
        fail(f"{action} Docker live mutation should not be ok: {result}")

    if result.get("blocked") is not True:
        fail(f"{action} Docker live mutation should be blocked: {result}")

    if result.get("mode") != "blocked_non_local_preview":
        fail(f"{action} wrong block mode: {result}")

    if result.get("runtime_context") != "docker":
        fail(f"{action} wrong runtime_context: {result}")

    if result.get("address_mode") != "docker_host":
        fail(f"{action} wrong address_mode: {result}")

    if result.get("live_runtime_mutation") is not False:
        fail(f"{action} must not mutate live runtime: {result}")

    if result.get("docker_apply") is not False:
        fail(f"{action} must not run Docker apply: {result}")

    if result.get("restart") is not False:
        fail(f"{action} must not restart: {result}")

    ok(f"Docker live {action} button is blocked safely")


def main() -> int:
    print("🧪 Verifying dashboard environment context safety")
    print("Scope: Docker dashboard live buttons must not mutate local runtime")

    for action in ["disable", "drain", "enable"]:
        assert_blocked_non_local(action)

    print("\n✅ DASHBOARD ENVIRONMENT CONTEXT SAFETY VERIFICATION PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
