from __future__ import annotations

import os
import ssl
import urllib.error
import urllib.request


BASE_HTTP = "http://127.0.0.1:8090"
BASE_HTTPS = "https://127.0.0.1:8443"


def fetch(
    url: str,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int | None, dict[str, str], str]:
    try:
        req = urllib.request.Request(url, data=body, method=method)

        for key, value in (headers or {}).items():
            req.add_header(key, value)

        context = None
        if url.startswith("https://"):
            context = ssl._create_unverified_context()

        with urllib.request.urlopen(req, timeout=10, context=context) as response:
            response_headers = {k: v for k, v in response.headers.items()}
            response_body = response.read().decode("utf-8", errors="replace")
            return int(response.status), response_headers, response_body

    except urllib.error.HTTPError as exc:
        response_headers = {k: v for k, v in exc.headers.items()}
        response_body = exc.read().decode("utf-8", errors="replace")
        return int(exc.code), response_headers, response_body

    except Exception as exc:
        return None, {}, f"{type(exc).__name__}: {exc}"



class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_no_redirect(url: str) -> tuple[int | None, dict[str, str], str]:
    try:
        opener = urllib.request.build_opener(NoRedirectHandler)
        req = urllib.request.Request(url)

        with opener.open(req, timeout=10) as response:
            headers = {k: v for k, v in response.headers.items()}
            body = response.read().decode("utf-8", errors="replace")
            return int(response.status), headers, body

    except urllib.error.HTTPError as exc:
        headers = {k: v for k, v in exc.headers.items()}
        body = exc.read().decode("utf-8", errors="replace")
        return int(exc.code), headers, body

    except Exception as exc:
        return None, {}, f"{type(exc).__name__}: {exc}"


def assert_ok(name: str, condition: bool, details: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name} failed. {details}")
    print(f"✅ {name}")


def main() -> int:
    print("🔐 Verifying NGINX edge security")

    api_key = os.getenv("PINGORA_EDGE_API_KEY", "dev-local-secret")
    api_headers = {"X-API-Key": api_key}

    # Root homepage is public over HTTPS.
    status, headers, body = fetch(f"{BASE_HTTPS}/")
    assert_ok("HTTPS homepage works", status == 200, f"status={status}, body={body[:200]}")

    # /api is protected without key.
    status, headers, body = fetch(f"{BASE_HTTPS}/api/")
    assert_ok("/api without API key rejected", status == 401, f"status={status}, body={body[:200]}")

    # /api works with key.
    status, headers, body = fetch(f"{BASE_HTTPS}/api/", headers=api_headers)
    assert_ok(
        "/api works with API key",
        status == 200 and ("DemoBackend" in body or "backend_port" in body),
        f"status={status}, body={body[:300]}",
    )

    # Secure headers should be present.
    required_headers = [
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Referrer-Policy",
        "Permissions-Policy",
        "Strict-Transport-Security",
    ]

    for header in required_headers:
        assert_ok(f"secure header present: {header}", header in headers, f"headers={headers}")

    # Blocked paths should be blocked before auth.
    blocked_paths = [
        "/.env",
        "/.git/config",
        "/wp-admin",
        "/phpmyadmin",
        "/admin.php",
        "/backup.sql",
    ]

    for path in blocked_paths:
        status, _, body = fetch(f"{BASE_HTTPS}{path}")
        assert_ok(f"blocked path over HTTPS {path}", status in {403, 404}, f"status={status}, body={body[:200]}")

    # HTTP should redirect to HTTPS. Do not follow the redirect here,
    # because local HTTPS uses a self-signed certificate.
    status, _, body = fetch_no_redirect(f"{BASE_HTTP}/")
    assert_ok("HTTP redirects to HTTPS", status in {301, 302, 307, 308}, f"status={status}, body={body[:200]}")

    # Large body should be rejected.
    big_body = b"x" * (12 * 1024 * 1024)
    status, _, body = fetch(
        f"{BASE_HTTPS}/api/",
        method="POST",
        body=big_body,
        headers={
            "X-API-Key": api_key,
            "Content-Type": "application/json",
            "Idempotency-Key": "edge-security-large-body-test",
        },
    )
    assert_ok("HTTPS large body rejected", status in {400, 413, 414, 499}, f"status={status}, body={body[:200]}")

    print("")
    print("✅ NGINX EDGE SECURITY VERIFICATION PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
