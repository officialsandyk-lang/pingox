from __future__ import annotations

import urllib.error
import urllib.request


LIVE_URL = "http://127.0.0.1:8090"

BLOCKED_PATHS = [
    "/.env",
    "/.git/config",
    "/wp-admin",
    "/phpmyadmin",
    "/admin.php",
    "/test.php",
    "/backup.sql",
    "/config.bak",
]

REQUIRED_HEADERS = [
    "X-Content-Type-Options",
    "X-Frame-Options",
    "Referrer-Policy",
    "Permissions-Policy",
]


def request(path: str, method: str = "GET", body: bytes | None = None):
    req = urllib.request.Request(LIVE_URL + path, data=body, method=method)

    if body is not None:
        req.add_header("Content-Type", "application/octet-stream")

    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return (
                int(response.status),
                dict(response.headers),
                response.read().decode("utf-8", errors="replace"),
            )
    except urllib.error.HTTPError as exc:
        return (
            int(exc.code),
            dict(exc.headers),
            exc.read().decode("utf-8", errors="replace"),
        )
    except Exception as exc:
        return None, {}, f"{type(exc).__name__}: {exc}"


def assert_ok(name: str, condition: bool, details: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name} failed. {details}")
    print(f"✅ {name}")


def main() -> int:
    print("🔐 Verifying NGINX edge security")

    status, headers, body = request("/api/")
    assert_ok(
        "/api still works",
        status == 200,
        f"status={status}, body={body[:200]}",
    )

    for header in REQUIRED_HEADERS:
        assert_ok(
            f"secure header present: {header}",
            header in headers,
            f"headers={headers}",
        )

    for path in BLOCKED_PATHS:
        status, headers, body = request(path)
        assert_ok(
            f"blocked path {path}",
            status in {403, 404},
            f"status={status}, body={body[:200]}",
        )

    large_body = b"x" * (11 * 1024 * 1024)
    status, headers, body = request(
        "/api/body-limit-test",
        method="POST",
        body=large_body,
    )

    assert_ok(
        "large body rejected",
        status in {413, 400, 403},
        f"status={status}, body={body[:200]}",
    )

    print("")
    print("✅ NGINX EDGE SECURITY VERIFICATION PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
