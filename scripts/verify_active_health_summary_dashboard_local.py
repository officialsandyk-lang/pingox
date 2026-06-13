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
    print("🧪 Verifying Active Health summary dashboard")

    html = get_text("/")

    required = [
        "activeHealthSummary",
        "activeHealthWorstBackend",
        "activeHealthLastError",
        "failingBackends",
        "Worst failing backend",
        "Last error summary",
    ]

    missing = [marker for marker in required if marker not in html]
    if missing:
        fail(f"dashboard missing Active Health summary markers: {missing}")

    print("✅ Active Health summary dashboard markers present")
    print("✅ ACTIVE HEALTH SUMMARY DASHBOARD LOCAL VERIFICATION PASSED")


if __name__ == "__main__":
    main()
