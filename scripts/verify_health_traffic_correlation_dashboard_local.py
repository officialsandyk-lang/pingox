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
    print("🧪 Verifying route traffic + active health dashboard correlation")

    html = get_text("/")

    required = [
        "healthTrafficCorrelationCard",
        "healthTrafficCorrelationList",
        "Route Impact",
        "data.route_traffic",
        "activeHealthUpstreams",
        "healthTrafficCorrelationListEl",
        "healthTrafficCorrelationListEl.innerHTML",
    ]

    missing = [marker for marker in required if marker not in html]
    if missing:
        fail(f"dashboard missing health/traffic correlation markers: {missing}")

    print("✅ Route traffic + active health correlation markers present")
    print("✅ HEALTH TRAFFIC CORRELATION DASHBOARD LOCAL VERIFICATION PASSED")


if __name__ == "__main__":
    main()
