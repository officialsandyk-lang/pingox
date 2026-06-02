from __future__ import annotations

import os
import ssl
import subprocess
import urllib.error
import urllib.request
from pathlib import Path


HTTP_URL = "http://127.0.0.1:8090"
HTTPS_URL = "https://127.0.0.1:8443"
API_KEY = os.getenv("PINGORA_EDGE_API_KEY", "dev-local-secret")

CERT_PATH = Path(os.getenv("PINGORA_TLS_CERT_PATH", "data/tls/local.crt"))
KEY_PATH = Path(os.getenv("PINGORA_TLS_KEY_PATH", "data/tls/local.key"))


def run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, text=True, capture_output=True)
    if result.returncode != 0:
        raise AssertionError(f"Command failed: {' '.join(cmd)}\n{result.stdout}\n{result.stderr}")
    return result.stdout + result.stderr


def request(url: str, *, api_key: str | None = None, method: str = "GET", body: bytes | None = None):
    ctx = ssl._create_unverified_context()
    req = urllib.request.Request(url, method=method, data=body)
    if api_key is not None:
        req.add_header("X-API-Key", api_key)
    if body is not None:
        req.add_header("Content-Type", "application/octet-stream")

    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as response:
            return int(response.status), dict(response.headers), response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), dict(exc.headers), exc.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return None, {}, f"{type(exc).__name__}: {exc}"


def request_no_redirect(url: str):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(url, method="GET")

    try:
        with opener.open(req, timeout=15) as response:
            return int(response.status), dict(response.headers), response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), dict(exc.headers), exc.read().decode("utf-8", errors="replace")


def assert_ok(name: str, condition: bool, details: str = ""):
    if not condition:
        raise AssertionError(f"{name} failed. {details}")
    print(f"✅ {name}")


def main() -> int:
    print("🔐 Verifying TLS/Auth/Secrets")

    assert_ok("cert file exists", CERT_PATH.exists(), str(CERT_PATH))
    assert_ok("key file exists", KEY_PATH.exists(), str(KEY_PATH))

    cert_info = run(["openssl", "x509", "-in", str(CERT_PATH), "-noout", "-issuer", "-subject", "-dates"])
    print(cert_info.strip())

    run(["openssl", "rsa", "-in", str(KEY_PATH), "-check", "-noout"])
    assert_ok("private key is valid", True)

    # cert/key match
    cert_mod = run(["openssl", "x509", "-noout", "-modulus", "-in", str(CERT_PATH)])
    key_mod = run(["openssl", "rsa", "-noout", "-modulus", "-in", str(KEY_PATH)])
    assert_ok("cert and key match", cert_mod.strip() == key_mod.strip())

    status, headers, body = request_no_redirect(HTTP_URL + "/api/")
    assert_ok("HTTP redirects to HTTPS", status in {301, 302, 307, 308}, f"status={status}, headers={headers}")

    status, headers, body = request(HTTPS_URL + "/api/")
    assert_ok("HTTPS without API key rejected", status == 401, f"status={status}, body={body[:200]}")

    status, headers, body = request(HTTPS_URL + "/api/", api_key="wrong-key")
    assert_ok("HTTPS wrong API key rejected", status == 401, f"status={status}, body={body[:200]}")

    status, headers, body = request(HTTPS_URL + "/api/", api_key=API_KEY)
    assert_ok("HTTPS correct API key reaches /api", status == 200, f"status={status}, body={body[:200]}")

    for header in ["X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy", "Permissions-Policy", "Strict-Transport-Security"]:
        assert_ok(f"secure header present: {header}", header in headers, f"headers={headers}")

    for path in ["/.env", "/.git/config", "/wp-admin", "/phpmyadmin", "/admin.php", "/backup.sql"]:
        status, headers, body = request(HTTPS_URL + path, api_key=API_KEY)
        assert_ok(f"blocked path over HTTPS {path}", status in {403, 404}, f"status={status}, body={body[:200]}")

    large_body = b"x" * (11 * 1024 * 1024)
    status, headers, body = request(HTTPS_URL + "/api/body-limit-test", api_key=API_KEY, method="POST", body=large_body)
    assert_ok("HTTPS large body rejected", status in {413, 400, 403}, f"status={status}, body={body[:200]}")

    print("")
    print("✅ TLS/AUTH/SECRETS VERIFICATION PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
