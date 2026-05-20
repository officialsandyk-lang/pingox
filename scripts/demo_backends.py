from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = PROJECT_ROOT / "tmp" / "logs"


def is_port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.4):
            return True
    except OSError:
        return False


def parse_port(address: Any) -> int | None:
    text = str(address or "").strip()
    text = text.replace("http://", "").replace("https://", "").rstrip("/")

    if "/" in text:
        text = text.split("/", 1)[0]

    if ":" not in text:
        return None

    try:
        port = int(text.rsplit(":", 1)[-1])
    except Exception:
        return None

    if 1 <= port <= 65535:
        return port

    return None


def collect_backend_ports(config: dict[str, Any]) -> list[int]:
    ports: list[int] = []
    seen: set[int] = set()

    routes = config.get("routes") or []

    if not isinstance(routes, list):
        return ports

    for route in routes:
        if not isinstance(route, dict):
            continue

        route_type = str(route.get("type") or "").lower()

        if route_type in {"static", "web", "webserver", "web_server", "file_server", "files"}:
            continue

        upstreams = route.get("upstreams")

        if isinstance(upstreams, list):
            for item in upstreams:
                if isinstance(item, dict):
                    port = parse_port(
                        item.get("address")
                        or item.get("upstream")
                        or item.get("backend")
                    )
                else:
                    port = parse_port(item)

                if port and port not in seen:
                    seen.add(port)
                    ports.append(port)

        else:
            port = parse_port(route.get("upstream") or route.get("backend"))

            if port and port not in seen:
                seen.add(port)
                ports.append(port)

    return ports


class DemoBackendHandler(BaseHTTPRequestHandler):
    server_version = "DemoBackend/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _read_body(self) -> str:
        length_raw = self.headers.get("Content-Length") or "0"

        try:
            length = int(length_raw)
        except Exception:
            length = 0

        if length <= 0:
            return ""

        return self.rfile.read(length).decode("utf-8", errors="replace")

    def _send(self, method: str, include_body: bool = True) -> None:
        port = int(self.server.server_port)
        request_body = self._read_body()

        response = {
            "service": "DemoBackend/1.0",
            "message": f"Demo Backend {port}",
            "backend_port": port,
            "method": method,
            "path": self.path,
            "headers": {
                key: value
                for key, value in self.headers.items()
                if key.lower()
                in {
                    "content-type",
                    "content-length",
                    "x-gateway-crud-probe",
                    "x-requested-with",
                    "idempotency-key",
                    "authorization",
                }
            },
            "body": request_body,
        }

        payload = json.dumps(response, indent=2).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload) if include_body else 0))
        self.send_header("X-Demo-Backend-Port", str(port))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS, HEAD")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, Idempotency-Key, X-Gateway-CRUD-Probe, X-Requested-With")
        self.end_headers()

        if include_body:
            self.wfile.write(payload)

    def do_GET(self) -> None:
        self._send("GET")

    def do_POST(self) -> None:
        self._send("POST")

    def do_PUT(self) -> None:
        self._send("PUT")

    def do_PATCH(self) -> None:
        self._send("PATCH")

    def do_DELETE(self) -> None:
        self._send("DELETE")

    def do_OPTIONS(self) -> None:
        self._send("OPTIONS")

    def do_HEAD(self) -> None:
        self._send("HEAD", include_body=False)

    def do_TRACE(self) -> None:
        self.send_response(405)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"TRACE not allowed")


def serve(port: int) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", int(port)), DemoBackendHandler)
    print(f"Demo Backend {port} listening on http://127.0.0.1:{port}/", flush=True)
    server.serve_forever()


def stop_ports(ports: list[int]) -> list[dict[str, Any]]:
    results = []

    for port in ports:
        command = f"lsof -tiTCP:{port} -sTCP:LISTEN | xargs -r kill -9"
        completed = subprocess.run(
            ["bash", "-lc", command],
            text=True,
            capture_output=True,
        )

        results.append(
            {
                "port": port,
                "stopped": completed.returncode == 0,
            }
        )

    return results


def start_ports(ports: list[int]) -> dict[str, Any]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    started = []
    already_running = []
    results = []

    for port in ports:
        if is_port_open(port):
            item = {
                "port": port,
                "status": "already_running",
                "started": False,
                "url": f"http://127.0.0.1:{port}/",
            }
            already_running.append(item)
            results.append(item)
            continue

        log_file = LOG_DIR / f"demo-backend-{port}.log"

        with log_file.open("a", encoding="utf-8", errors="replace") as log:
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "serve", str(port)],
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                text=True,
                start_new_session=True,
            )

        item = {
            "port": port,
            "status": "started",
            "started": True,
            "pid": process.pid,
            "url": f"http://127.0.0.1:{port}/",
            "log_file": str(log_file),
        }

        started.append(item)
        results.append(item)

    return {
        "ports": ports,
        "started": started,
        "already_running": already_running,
        "results": results,
    }


def load_config(config_path: str | Path) -> dict[str, Any]:
    return json.loads(Path(config_path).read_text(encoding="utf-8"))


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python scripts/demo_backends.py start generated-pingora-proxy/config.json")
        print("  python scripts/demo_backends.py restart generated-pingora-proxy/config.json")
        print("  python scripts/demo_backends.py stop generated-pingora-proxy/config.json")
        print("  python scripts/demo_backends.py serve 9101")
        return 2

    command = sys.argv[1]

    if command == "serve":
        if len(sys.argv) < 3:
            raise SystemExit("Missing port")
        serve(int(sys.argv[2]))
        return 0

    if len(sys.argv) < 3:
        raise SystemExit("Missing config path")

    config = load_config(sys.argv[2])
    ports = collect_backend_ports(config)

    if command == "start":
        print(json.dumps(start_ports(ports), indent=2))
        return 0

    if command == "stop":
        print(json.dumps({"stopped": stop_ports(ports)}, indent=2))
        return 0

    if command == "restart":
        stopped = stop_ports(ports)
        started = start_ports(ports)
        print(json.dumps({"stopped": stopped, **started}, indent=2))
        return 0

    raise SystemExit(f"Unknown command: {command}")


if __name__ == "__main__":
    raise SystemExit(main())
