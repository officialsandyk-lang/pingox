#!/usr/bin/env python3
from __future__ import annotations

import json
import urllib.request


DASHBOARD = "http://127.0.0.1:8080/"
CONTROL = "http://127.0.0.1:8787"
TOKEN = "dev-local-control-token"


def fail(message: str) -> None:
    raise SystemExit(f"❌ {message}")


def get_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.read().decode("utf-8", errors="replace")


def get_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"X-Pingora-Control-Token": TOKEN},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> None:
    print("🧪 Verifying Agent Learning dashboard")

    data = get_json(f"{CONTROL}/__gateway_control/agent/learnings?limit=5")
    if not data.get("ok"):
        fail(f"agent learnings endpoint failed: {data}")

    if "learnings" not in data:
        fail("agent learnings endpoint missing learnings")

    html = get_text(DASHBOARD)
    required = [
        "agentLearningCard",
        "agentLearningList",
        "fetchAgentLearnings",
        "Agent Learning",
        "/__gateway_control/agent/learnings",
    ]
    missing = [marker for marker in required if marker not in html]
    if missing:
        fail(f"dashboard missing markers: {missing}")

    print("✅ Agent learning endpoint works")
    print("✅ Agent Learning dashboard markers present")
    print("✅ AGENT LEARNING DASHBOARD LOCAL VERIFICATION PASSED")


if __name__ == "__main__":
    main()
