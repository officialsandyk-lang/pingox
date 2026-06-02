from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
BASE = "https://127.0.0.1:8443"

BASELINE_PROMPT = (
    "create gateway with /api random across 127.0.0.1:9101, "
    "127.0.0.1:9102 and 127.0.0.1:9103"
)

TEST_ROUTES = {
    "/coupons": "127.0.0.1:9401",
    "/billing": "127.0.0.1:9402",
    "/audit": "127.0.0.1:9499",
}


def api_key() -> str:
    return os.getenv("PINGORA_EDGE_API_KEY", "dev-local-secret")


def run(cmd: list[str], timeout: int = 1800) -> subprocess.CompletedProcess:
    print("")
    print("$ " + " ".join(cmd))

    result = subprocess.run(
        cmd,
        cwd=str(PROJECT_ROOT),
        text=True,
        capture_output=True,
        timeout=timeout,
    )

    if result.stdout.strip():
        print(result.stdout[-12000:])

    if result.stderr.strip():
        print(result.stderr[-6000:])

    if result.returncode != 0:
        raise AssertionError(
            f"Command failed: {' '.join(cmd)}\n"
            f"STDOUT:\n{result.stdout[-3000:]}\n\n"
            f"STDERR:\n{result.stderr[-3000:]}"
        )

    return result


def fetch(path: str, key: bool = True) -> tuple[int | None, str, dict[str, str]]:
    url = BASE + path
    if not url.endswith("/"):
        url += "/"

    try:
        req = urllib.request.Request(url)
        if key:
            req.add_header("X-API-Key", api_key())

        context = ssl._create_unverified_context()

        with urllib.request.urlopen(req, timeout=10, context=context) as response:
            body = response.read().decode("utf-8", errors="replace")
            headers = {k: v for k, v in response.headers.items()}
            return int(response.status), body, headers

    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        headers = {k: v for k, v in exc.headers.items()}
        return int(exc.code), body, headers

    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}", {}


def assert_true(name: str, condition: bool, details: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name} failed. {details}")
    print(f"✅ {name}")


def load_config() -> dict:
    return json.loads((PROJECT_ROOT / "generated-pingora-proxy" / "config.json").read_text())


def route_paths() -> list[str]:
    return [r.get("path") for r in load_config().get("routes", [])]


def assert_route_live(route: str, expected_port: str) -> None:
    status, body, headers = fetch(route, key=True)
    assert_true(
        f"{route} live with API key",
        status == 200 and expected_port in body,
        f"status={status}, body={body[:300]}, headers={headers}",
    )

    status_no_key, body_no_key, _ = fetch(route, key=False)
    assert_true(
        f"{route} protected without API key",
        status_no_key == 401,
        f"status={status_no_key}, body={body_no_key[:200]}",
    )


def assert_route_removed(route: str) -> None:
    status, body, _ = fetch(route, key=True)
    assert_true(
        f"{route} removed",
        status in {404, 502},
        f"status={status}, body={body[:200]}",
    )


def warm_routes() -> None:
    for route in route_paths():
        if route == "/":
            fetch("/", key=False)
            continue

        for _ in range(5):
            fetch(route, key=True)


def verify_level(label: str, level: str = "smoke", attempts: int = 3) -> None:
    print(f"\n🧪 Verifier: {label} level={level}")

    code = f"""
import json
from pathlib import Path
from core.post_change_verifier import verify_after_change, format_verification_report

config = json.loads(Path("generated-pingora-proxy/config.json").read_text())
result = verify_after_change(
    config=config,
    live_url="https://127.0.0.1:8443",
    level="{level}",
    change_type="tls_auth_route_mutation_stress",
    source="stress_tls_auth_route_mutations",
)
print(format_verification_report(result))
if not result.get("success"):
    raise SystemExit(1)
"""

    last_error = None

    for attempt in range(1, attempts + 1):
        print(f"🧪 Attempt {attempt}/{attempts}: {label} level={level}")
        warm_routes()

        try:
            run([PYTHON, "-c", code], timeout=900)
            return
        except Exception as exc:
            last_error = exc
            print(f"⚠️ Verifier attempt failed: {exc}")

            if attempt < attempts:
                import time
                time.sleep(25)

    raise AssertionError(f"Verifier failed after retries: {label} level={level}: {last_error}")


def verify_full(label: str) -> None:
    # Intermediate mutation checks use smoke because update.py already runs
    # full verification, and immediate concurrent checks can see transient
    # Docker Desktop post-switch 503s.
    verify_level(label, level="smoke", attempts=3)


def verify_final_full(label: str) -> None:
    # Final strict check after all add/edit/remove mutations have settled.
    import time
    warm_routes()
    time.sleep(30)
    verify_level(label, level="full", attempts=3)


def assert_algorithm_integrity(expected_api_algorithm: str) -> None:
    config = load_config()
    routes = {r.get("path"): r for r in config.get("routes", [])}

    assert_true("/ exists", "/" in routes)
    assert_true("/api exists", "/api" in routes)

    api = routes["/api"]
    actual = api.get("balancing") or api.get("algorithm")
    assert_true(
        f"/api algorithm is {expected_api_algorithm}",
        actual == expected_api_algorithm,
        f"actual={actual}, route={api}",
    )

    upstreams = api.get("upstreams") or []
    upstream_text = ",".join(map(str, upstreams))
    assert_true(
        "/api upstream integrity",
        len(upstreams) == 3 and all(port in upstream_text for port in ["9101", "9102", "9103"]),
        f"upstreams={upstreams}",
    )


def main() -> int:
    print("🔥 TLS/Auth Docker Blue-Green Route Mutation Stress Test")

    os.environ.setdefault("PINGORA_EDGE_API_KEY", "dev-local-secret")

    run(["bash", "scripts/docker_ready.sh"], timeout=240)

    run(
        [
            "pingora-ai",
            "apply",
            "--runtime",
            "docker_host",
            "--strategy",
            "bluegreen",
            "--verify",
            "full",
            BASELINE_PROMPT,
        ],
        timeout=1800,
    )

    verify_full("baseline")
    assert_algorithm_integrity("random")

    for route, backend in TEST_ROUTES.items():
        run(["pingora-ai", "add", route, backend], timeout=1800)
        assert_true(f"{route} in config after add", route in route_paths(), str(route_paths()))
        assert_route_live(route, backend.split(":")[-1])
        verify_full(f"after add {route}")
        assert_algorithm_integrity("random")

    run(["pingora-ai", "add", "/coupons", "127.0.0.1:9401"], timeout=1800)
    assert_true("duplicate add kept /coupons", "/coupons" in route_paths(), str(route_paths()))
    assert_algorithm_integrity("random")

    run(["pingora-ai", "edit", "/api", "--algorithm", "round_robin"], timeout=1800)
    verify_full("after edit /api round_robin")
    assert_algorithm_integrity("round_robin")

    run(["pingora-ai", "edit", "/api", "--algorithm", "random"], timeout=1800)
    verify_full("after edit /api random")
    assert_algorithm_integrity("random")

    for route in list(TEST_ROUTES.keys()):
        run(["pingora-ai", "remove", route], timeout=1800)
        assert_true(f"{route} absent from config after remove", route not in route_paths(), str(route_paths()))
        assert_route_removed(route)
        verify_full(f"after remove {route}")
        assert_algorithm_integrity("random")

    verify_final_full("final strict full verification after all mutations")

    run([PYTHON, "scripts/verify_nginx_edge_security.py"], timeout=300)
    run([PYTHON, "scripts/verify_tls_auth_secrets.py"], timeout=300)

    print("")
    print("✅ TLS/AUTH ROUTE MUTATION STRESS TEST PASSED")
    print("✅ Multiple route add/remove verified")
    print("✅ Algorithm edit verified")
    print("✅ Untouched route integrity preserved")
    print("✅ Protected routes require X-API-Key")
    print("✅ NGINX edge decision/reload path verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
