from __future__ import annotations

import json
import re
import sys
import time
import urllib.request


BASE = "http://127.0.0.1:8080"
ROUTE = "/api"
TARGET = "127.0.0.1:9101"
REMAINING = "9102"


def get_text(url: str, timeout: int = 5) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as res:
        return res.read().decode("utf-8", errors="replace")


def post_json(url: str, timeout: int = 5) -> dict:
    req = urllib.request.Request(url, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def backend_counts(n: int = 30) -> dict[str, int]:
    counts: dict[str, int] = {}
    probe_id = int(time.time() * 1000)

    for i in range(n):
        # Cache-bust each request so this verifier measures live upstream
        # selection, not cached backend evidence.
        body = get_text(f"{BASE}{ROUTE}/?runtime_admin_probe={probe_id}_{i}")
        matches = re.findall(r'"backend_port"\s*:\s*(\d+)', body)

        if not matches:
            matches = re.findall(r"Demo Backend\s+(\d+)", body)

        if not matches:
            matches = re.findall(r"MARKER_BACKEND_(\d+)", body)

        if not matches:
            matches = re.findall(r"NAME=backend-(\d+)", body)

        for port in matches:
            counts[port] = counts.get(port, 0) + 1

    return counts


def require(condition: bool, message: str) -> None:
    if not condition:
        print(f"❌ {message}")
        raise SystemExit(1)


def main() -> int:
    print("🧪 Verifying local Runtime Admin Agent")

    state = json.loads(get_text(f"{BASE}/__gateway_runtime_state"))
    require(state.get("ok") is True, "runtime_state ok must be true")

    print("✅ runtime_state reachable")

    # Ensure target is enabled first.
    enable = post_json(
        f"{BASE}/__gateway_runtime_admin/upstream/enable"
        f"?route={ROUTE}&upstream={TARGET}&confirm=true"
    )
    require(enable.get("ok") is True, "enable before test failed")
    print("✅ pre-test enable passed")

    time.sleep(1)

    before = backend_counts(30)
    print("Before disable/drain counts:", before)
    require("9101" in before and "9102" in before, "both 9101 and 9102 should receive traffic before mutation")

    disable = post_json(
        f"{BASE}/__gateway_runtime_admin/upstream/disable"
        f"?route={ROUTE}&upstream={TARGET}&confirm=true"
    )
    print("Disable result:", json.dumps(disable, indent=2))
    require(disable.get("ok") is True, "disable failed")
    require(disable.get("live_runtime_mutation") is True, "disable must be live runtime mutation")
    require(disable.get("restart") is False, "disable must not restart")
    require(disable.get("docker_apply") is False, "disable must not docker apply")

    after_disable = backend_counts(30)
    print("After disable counts:", after_disable)
    require(after_disable.get("9101", 0) == 0, "9101 must receive 0 traffic after disable")
    require(after_disable.get("9102", 0) > 0, "9102 must receive traffic after disable")

    enable2 = post_json(
        f"{BASE}/__gateway_runtime_admin/upstream/enable"
        f"?route={ROUTE}&upstream={TARGET}&confirm=true"
    )
    print("Enable result:", json.dumps(enable2, indent=2))
    require(enable2.get("ok") is True, "enable after disable failed")

    time.sleep(1)

    after_enable = backend_counts(30)
    print("After enable counts:", after_enable)
    require(after_enable.get("9101", 0) > 0, "9101 must receive traffic again after enable")
    require(after_enable.get("9102", 0) > 0, "9102 must receive traffic after enable")

    drain = post_json(
        f"{BASE}/__gateway_runtime_admin/upstream/drain"
        f"?route={ROUTE}&upstream={TARGET}&confirm=true"
    )
    print("Drain result:", json.dumps(drain, indent=2))
    require(drain.get("ok") is True, "drain failed")
    require(drain.get("live_runtime_mutation") is True, "drain must be live runtime mutation")

    after_drain = backend_counts(30)
    print("After drain counts:", after_drain)
    require(after_drain.get("9101", 0) == 0, "9101 must receive 0 traffic after drain")
    require(after_drain.get("9102", 0) > 0, "9102 must receive traffic after drain")

    # Restore state.
    restore = post_json(
        f"{BASE}/__gateway_runtime_admin/upstream/enable"
        f"?route={ROUTE}&upstream={TARGET}&confirm=true"
    )
    require(restore.get("ok") is True, "restore enable failed")

    print("✅ LOCAL RUNTIME ADMIN VERIFIER PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
