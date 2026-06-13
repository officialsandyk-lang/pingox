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
    print("🧪 Verifying Active Health dashboard alert banner")

    html = get_text("/")

    required = [
        "activeHealthAlertBanner",
        "activeHealthAlertText",
        "Active Health Alert",
        "affectedRoutes",
        "unhealthy backend target",
        "affected route",
        "Worst:",
    ]

    missing = [marker for marker in required if marker not in html]
    if missing:
        fail(f"dashboard missing Active Health alert markers: {missing}")

    print("✅ Active Health alert banner markers present")
    print("✅ ACTIVE HEALTH ALERT BANNER DASHBOARD LOCAL VERIFICATION PASSED")


if __name__ == "__main__":
    main()
