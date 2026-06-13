#!/usr/bin/env python3
from __future__ import annotations

import urllib.request


BASE = "http://127.0.0.1:8080"


def fail(message: str) -> None:
    raise SystemExit(f"❌ {message}")


def get_text(path: str) -> str:
    with urllib.request.urlopen(BASE + path, timeout=10) as response:
        return response.read().decode("utf-8", errors="replace")


def main() -> None:
    print("🧪 Verifying dashboard auto-refresh markers")

    html = get_text("/")

    required = [
        "runtimeStateLastUpdated",
        "new Date().toLocaleTimeString()",
        "updateCacheCountersFromRuntimeState();",
        "setInterval(updateCacheCountersFromRuntimeState, 5000)",
    ]

    missing = [marker for marker in required if marker not in html]
    if missing:
        fail(f"dashboard missing auto-refresh markers: {missing}")

    if "setInterval(fetchRuntimeState, 5000)" in html or "window.setInterval(fetchRuntimeState, 5000)" in html:
        fail("dashboard still contains wrong fetchRuntimeState interval")

    print("✅ Dashboard auto-refresh markers present")
    print("✅ DASHBOARD AUTO-REFRESH LOCAL VERIFICATION PASSED")


if __name__ == "__main__":
    main()
