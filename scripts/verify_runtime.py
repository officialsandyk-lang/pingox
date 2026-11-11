"""Real generated-binary regression tests. No AI, Docker, or external backends.
Run from repository root: python scripts/verify_runtime.py
"""
from __future__ import annotations
import concurrent.futures
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.project_writer import write_project


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


held = threading.Event()
release = threading.Event()


class Backend(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        if self.path.startswith("/hold"):
            held.set()
            release.wait(5)
        if self.path.startswith("/slow"):
            time.sleep(2)
        body = json.dumps({"port": self.server.server_port, "ip": self.headers.get("x-real-ip")}).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError): pass
    def do_POST(self):
        try:
            if self.headers.get("Transfer-Encoding") == "chunked":
                while True:
                    line = self.rfile.readline()
                    if not line: return
                    size = int(line.strip(), 16)
                    if size == 0:
                        self.rfile.readline()
                        break
                    self.rfile.read(size + 2)
            else:
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.do_GET()
        except (BrokenPipeError, ConnectionResetError): pass


def main():
    servers = [ThreadingHTTPServer(("127.0.0.1", 0), Backend) for _ in range(2)]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    ports = [s.server_port for s in servers]
    gateway_port = free_port()
    project = Path("tmp/runtime-regression").resolve()
    pool = [{"address": f"127.0.0.1:{p}", "weight": 1} for p in ports]
    routes = [{"path": path, "upstreams": pool, "balancing": algorithm}
              for path, algorithm in [("/rr", "round_robin"), ("/least", "least_connections"), ("/ip", "ip_hash")]]
    routes += [{"path": "/weighted", "balancing": "weighted_round_robin", "upstreams": [
        {"address": pool[0]["address"], "weight": 2}, {"address": pool[1]["address"], "weight": 1}]}]
    routes += [{"path": p, "upstream": pool[0]["address"]} for p in ["/post", "/hold", "/slow", "/private"]]
    routes += [{"path": "/files", "type": "static", "root": "public", "index": "index.html"},
               {"path": "/down", "upstream": f"127.0.0.1:{free_port()}"}]
    write_project({"port": gateway_port, "routes": routes, "demo_backend": {"enabled": False},
                   "security": {"max_connections": 2, "max_request_body_bytes": 16,
                                "upstream_timeout_seconds": 1, "rate_limit_per_minute": 100,
                                "blocked_paths": ["/private"], "allowed_methods": ["GET", "HEAD", "POST"]}}, project)
    (project / "secret.txt").write_text("must never be served")
    link = project / "public" / "escape.txt"
    if not link.is_symlink(): link.symlink_to(project / "secret.txt")
    env = dict(os.environ)
    env.pop("TRUSTED_PROXY_CIDRS", None)
    env.setdefault("CARGO_TARGET_DIR", str(Path("tmp/cargo-target").resolve()))
    subprocess.run(["cargo", "build", "--manifest-path", str(project / "Cargo.toml")], env=env, check=True)
    binary = Path(env["CARGO_TARGET_DIR"]) / "debug/generated-pingora-proxy"
    log = (project / "runtime.log").open("w")
    process = subprocess.Popen([str(binary)], cwd=project, env=env, stdout=log, stderr=log)

    def request(path, method="GET", body=None, headers=None, chunked=False):
        conn = http.client.HTTPConnection("127.0.0.1", gateway_port, timeout=4)
        try:
            conn.request(method, path, body=body, headers=headers or {}, encode_chunked=chunked)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()
    try:
        for _ in range(100):
            try:
                if request("/_gateway/health")[0] == 200: break
            except OSError: pass
            if process.poll() is not None: raise AssertionError("gateway exited during startup")
            time.sleep(.1)
        else: raise AssertionError("gateway readiness timed out")
        assert request("/private")[0] == 403
        assert request("/pr%69vate")[0] == 400
        assert request("/rr/../private")[0] == 400
        assert request("/rr", "DELETE")[0] == 405
        assert request("/missing")[0] == 404
        assert request("/files/escape.txt")[0] == 403
        assert request("/files/")[0] == 200
        assert request("/files/", "HEAD")[1] == b""
        assert request("/post", "POST", b"small")[0] == 200
        assert request("/post", "POST", b"x" * 17)[0] == 413
        assert request("/post", "POST", iter([b"x" * 10, b"x" * 10]), chunked=True)[0] == 413
        assert request("/down")[0] == 502
        for route in ("/rr", "/least"):
            used = [json.loads(request(route)[1])["port"] for _ in range(6)]
            assert used.count(ports[0]) == used.count(ports[1]) == 3, (route, used)
        weighted = [json.loads(request("/weighted")[1])["port"] for _ in range(6)]
        assert weighted.count(ports[0]) == 4 and weighted.count(ports[1]) == 2
        actual = json.loads(request("/rr", headers={"X-Real-IP": "1.2.3.4", "X-Forwarded-For": "1.2.3.4"})[1])
        assert actual["ip"] == "127.0.0.1"
        with concurrent.futures.ThreadPoolExecutor() as executor:
            first = executor.submit(request, "/hold")
            assert held.wait(2)
            second = executor.submit(request, "/hold")
            time.sleep(.15)
            assert request("/rr")[0] == 503
            release.set()
            assert first.result()[0] == second.result()[0] == 200
        assert request("/rr")[0] == 200, "admission count leaked"
        start = time.monotonic()
        assert request("/slow")[0] >= 500
        assert time.monotonic() - start < 1.8, "upstream timeout not applied"
        assert request("/rr")[0] == 200, "error leaked active count"
        statuses = [request("/rr")[0] for _ in range(105)]
        assert 429 in statuses
        assert request("/_gateway/health")[0] == 200, "health must bypass traffic rate limits"
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=40)
        assert process.returncode == 0
        print("PASS: live routing, weighting, request limits, encoded paths, static containment, spoofed headers, failure recovery, health and shutdown")
    finally:
        release.set()
        if process.poll() is None:
            process.terminate()
            try: process.wait(timeout=40)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        log.close()
        for server in servers: server.shutdown(); server.server_close()


if __name__ == "__main__": main()
