#!/usr/bin/env python3
from __future__ import annotations

import json
import urllib.request


CONTROL = "http://127.0.0.1:8787"
DASHBOARD = "http://127.0.0.1:8080/"
TOKEN = "dev-local-control-token"


def fail(message: str) -> None:
    raise SystemExit(f"❌ {message}")


def get_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"X-Pingora-Control-Token": TOKEN},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def get_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.read().decode("utf-8", errors="replace")


def main() -> None:
    print("🧪 Verifying Agent Learning actionability")

    data = get_json(f"{CONTROL}/__gateway_control/agent/learnings?limit=5")
    if not data.get("ok"):
        fail(f"learning endpoint failed: {data}")

    rows = data.get("learnings") or []
    if not rows:
        fail("no learnings returned")

    required = {
        "actionability",
        "safe_to_auto_repair",
        "requires_confirmation",
        "suggested_action",
        "safe_command_preview",
        "reason",
    }

    for row in rows:
        missing = required - set(row.keys())
        if missing:
            fail(f"learning row missing fields {sorted(missing)}: {row}")

    html = get_text(DASHBOARD)
    markers = [
        "actionability",
        "auto-repair",
        "confirm",
        "command preview",
        "reason",
    ]
    missing_markers = [marker for marker in markers if marker not in html]
    if missing_markers:
        fail(f"dashboard missing actionability markers: {missing_markers}")

    print("✅ Actionability fields returned by Control API")
    print("✅ Dashboard actionability markers present")
    print("✅ AGENT LEARNING ACTIONABILITY LOCAL VERIFICATION PASSED")


if __name__ == "__main__":
    main()
