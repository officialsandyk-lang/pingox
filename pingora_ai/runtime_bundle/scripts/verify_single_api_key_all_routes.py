from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://127.0.0.1:8443"
KEY = os.getenv("PINGORA_EDGE_API_KEY", "dev-local-secret")
CONFIG = Path("generated-pingora-proxy/config.json")


def fetch(path: str, with_key: bool) -> int | None:
    if not path.endswith("/"):
        path += "/"

    req = urllib.request.Request(BASE + path)

    if with_key:
        req.add_header("X-API-Key", KEY)

    try:
        context = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=10, context=context) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except Exception as exc:
        print(f"ERROR {path}: {type(exc).__name__}: {exc}")
        return None


def main() -> int:
    config = json.loads(CONFIG.read_text())
    routes = [r for r in config.get("routes", []) if r.get("path")]

    print("🔐 Verifying single API key across all routes")
    print(f"Routes: {[r.get('path') for r in routes]}")

    failures = []

    for route in routes:
        path = route.get("path")
        route_type = route.get("type")

        if path == "/":
            status = fetch(path, with_key=False)
            ok = status == 200
            print(f"{'✅' if ok else '❌'} {path} public without key -> {status}")
            if not ok:
                failures.append((path, "public_root", status))
            continue

        no_key_status = fetch(path, with_key=False)
        key_status = fetch(path, with_key=True)

        no_key_ok = no_key_status == 401
        key_ok = key_status == 200

        print(f"{'✅' if no_key_ok else '❌'} {path} without key -> {no_key_status}")
        print(f"{'✅' if key_ok else '❌'} {path} with key -> {key_status}")

        if not no_key_ok:
            failures.append((path, "without_key_should_be_401", no_key_status))

        if not key_ok:
            failures.append((path, "with_key_should_be_200", key_status))

    if failures:
        print("\n❌ Single API key route test failed:")
        for item in failures:
            print(item)
        return 1

    print("\n✅ SINGLE API KEY ALL ROUTES TEST PASSED")
    print("✅ / is public")
    print("✅ all protected routes reject missing key")
    print("✅ all protected routes accept the same valid API key")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
