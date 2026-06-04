from __future__ import annotations

from pathlib import Path
from typing import Any


def repair_local_live_url_to_8090() -> dict[str, Any]:
    return {
        "ok": True,
        "repair_action": "set_local_live_url_to_8090",
        "live_url": "http://127.0.0.1:8090",
        "note": "Use this value for local/direct post-change verification.",
    }


def repair_local_readiness_to_homepage() -> dict[str, Any]:
    path = Path("core/environment_deployer.py")
    text = path.read_text(encoding="utf-8")

    changed = "__edge_health" in text

    text = text.replace(
        '"http://127.0.0.1:8090/__edge_health"',
        '"http://127.0.0.1:8090/"',
    )

    path.write_text(text, encoding="utf-8")

    return {
        "ok": True,
        "repair_action": "set_local_readiness_to_homepage",
        "changed": changed,
    }


def repair_bare_metal_upstreams(config: dict[str, Any]) -> dict[str, Any]:
    for route in config.get("routes", []) or []:
        if not isinstance(route, dict):
            continue

        if isinstance(route.get("upstreams"), list):
            route["upstreams"] = [
                str(item).replace("host.docker.internal", "127.0.0.1")
                for item in route["upstreams"]
            ]

        if route.get("upstream"):
            route["upstream"] = str(route["upstream"]).replace(
                "host.docker.internal",
                "127.0.0.1",
            )

    return {
        "ok": True,
        "repair_action": "rewrite_docker_host_to_loopback",
        "config": config,
    }


def repair_from_diagnosis(
    diagnosis: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    action = diagnosis.get("repair_action")

    if action == "set_local_live_url_to_8090":
        return repair_local_live_url_to_8090()

    if action == "set_local_readiness_to_homepage":
        return repair_local_readiness_to_homepage()

    if action == "rewrite_docker_host_to_loopback":
        if config is None:
            return {
                "ok": False,
                "reason": "config required for rewrite_docker_host_to_loopback",
            }
        return repair_bare_metal_upstreams(config)

    if action == "upload_static_assets_to_release":
        return {
            "ok": True,
            "repair_action": action,
            "note": "Handled by bare-metal deployer static asset packaging.",
        }

    if action == "start_missing_demo_backends_for_config":
        return repair_start_missing_demo_backends_for_config(config)

    if action == "kill_process_then_reset_failed":
        return {
            "ok": True,
            "repair_action": action,
            "note": "Handled by bare-metal deployer _stop_service.",
        }

    return {
        "ok": False,
        "reason": f"No deterministic repair registered for action: {action}",
    }

def repair_start_missing_demo_backends_for_config(config: dict | None = None) -> dict:
    import subprocess
    import sys
    import time
    import urllib.request
    from pathlib import Path

    ports = set()

    if isinstance(config, dict):
        for route in config.get("routes", []) or []:
            if not isinstance(route, dict):
                continue

            for key in ("upstream", "backend"):
                value = route.get(key)
                if isinstance(value, str) and ":" in value:
                    try:
                        ports.add(int(value.rsplit(":", 1)[1]))
                    except Exception:
                        pass

            for value in route.get("upstreams", []) or []:
                if isinstance(value, str) and ":" in value:
                    try:
                        ports.add(int(value.rsplit(":", 1)[1]))
                    except Exception:
                        pass
                elif isinstance(value, dict):
                    address = str(value.get("address") or "")
                    if ":" in address:
                        try:
                            ports.add(int(address.rsplit(":", 1)[1]))
                        except Exception:
                            pass

    if not ports:
        ports = {9101, 9102, 9103}

    helper = Path("tmp/docker_auto_backend.py")
    helper.parent.mkdir(parents=True, exist_ok=True)
    helper.write_text(r'''
from http.server import BaseHTTPRequestHandler, HTTPServer
import json, sys

PORT = int(sys.argv[1])

class H(BaseHTTPRequestHandler):
    def do_GET(self): self.reply()
    def do_POST(self): self.reply()
    def do_PUT(self): self.reply()
    def do_PATCH(self): self.reply()
    def do_DELETE(self): self.reply()
    def do_OPTIONS(self): self.reply()
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("X-Demo-Backend-Port", str(PORT))
        self.end_headers()

    def reply(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "ignore") if length else ""
        data = json.dumps({
            "server": "DemoBackend/1.0",
            "backend_port": PORT,
            "path": self.path,
            "method": self.command,
            "body": body,
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Demo-Backend-Port", str(PORT))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

HTTPServer(("127.0.0.1", PORT), H).serve_forever()
''')

    started = []
    healthy = []

    for port in sorted(ports):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
            healthy.append(port)
            continue
        except Exception:
            pass

        subprocess.Popen(
            [sys.executable, str(helper), str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        started.append(port)

    time.sleep(2)

    failed = []
    for port in sorted(ports):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3)
            if port not in healthy:
                healthy.append(port)
        except Exception as exc:
            failed.append({"port": port, "error": str(exc)})

    return {
        "ok": not failed,
        "repair_action": "start_missing_demo_backends_for_config",
        "ports": sorted(ports),
        "started": started,
        "healthy": sorted(set(healthy)),
        "failed": failed,
    }
