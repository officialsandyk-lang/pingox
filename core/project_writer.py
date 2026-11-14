from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_DIR = PROJECT_ROOT / "generated-pingora-proxy"


DEFAULT_SECURITY = {
    "blocked_paths": [],
    "allowed_methods": ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    "rate_limit_per_minute": 120,
    "max_connections": 1000,
    "max_request_body_bytes": 1048576,
    "upstream_timeout_seconds": 30,
}


DEFAULT_DEMO_BACKEND = {
    "enabled": True,
    "generate_placeholder_files": True,
    "overwrite_existing": False,
}


VALID_BALANCING_ALGORITHMS = {
    "round_robin",
    "random",
    "weighted_round_robin",
    "least_connections",
    "ip_hash",
}


def rust_string(value: Any) -> str:
    return json.dumps(str(value))


def normalize_path(path: Any) -> str:
    path = str(path or "/").strip()

    if not path.startswith("/"):
        path = "/" + path

    path = re.sub(r"[^a-zA-Z0-9/_\-.]", "", path)

    if not path:
        return "/"

    if not path.startswith("/"):
        path = "/" + path

    if len(path) > 1:
        path = path.rstrip("/")

    return path


def normalize_port(value: Any, default: int = 9000) -> int:
    try:
        port = int(value)
    except Exception:
        return default

    if 1 <= port <= 65535:
        return port

    return default


def normalize_upstream_address(value: Any) -> str:
    upstream = str(value or "127.0.0.1:3000").strip()

    upstream = upstream.replace("http://", "")
    upstream = upstream.replace("https://", "")
    upstream = upstream.rstrip("/")

    if "/" in upstream:
        upstream = upstream.split("/", 1)[0]

    if ":" not in upstream:
        return "127.0.0.1:3000"

    host, port_text = upstream.rsplit(":", 1)

    host = host.strip()
    port = normalize_port(port_text, default=3000)

    if host == "localhost":
        host = "127.0.0.1"

    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", host):
        host = "127.0.0.1"

    return f"{host}:{port}"


def normalize_balancing(value: Any) -> str:
    text = str(value or "round_robin").strip().lower()
    text = text.replace("-", "_").replace(" ", "_")

    aliases = {
        "rr": "round_robin",
        "roundrobin": "round_robin",
        "round_robin": "round_robin",
        "random": "random",
        "rand": "random",
        "weighted": "weighted_round_robin",
        "weighted_rr": "weighted_round_robin",
        "weighted_roundrobin": "weighted_round_robin",
        "weighted_round_robin": "weighted_round_robin",
        "wrr": "weighted_round_robin",
        "least_connection": "least_connections",
        "least_connections": "least_connections",
        "least_conn": "least_connections",
        "leastconn": "least_connections",
        "lc": "least_connections",
        "ip_hash": "ip_hash",
        "iphash": "ip_hash",
        "source_ip_hash": "ip_hash",
        "sticky": "ip_hash",
        "sticky_session": "ip_hash",
    }

    normalized = aliases.get(text, "round_robin")

    if normalized not in VALID_BALANCING_ALGORITHMS:
        return "round_robin"

    return normalized


def is_static_route(route: dict[str, Any]) -> bool:
    route_type = str(
        route.get("type")
        or route.get("kind")
        or route.get("mode")
        or ""
    ).strip().lower()

    return route_type in {
        "static",
        "web",
        "webserver",
        "web_server",
        "file_server",
        "files",
    }


def normalize_static_route(route: dict[str, Any]) -> dict[str, Any]:
    path = normalize_path(
        route.get("path")
        or route.get("prefix")
        or route.get("route")
        or "/"
    )

    fixed = dict(route)
    fixed["path"] = path
    fixed["type"] = "static"
    fixed["root"] = str(
        route.get("root")
        or route.get("dir")
        or route.get("directory")
        or "public"
    )
    fixed["index"] = str(route.get("index") or "index.html")

    for key in (
        "upstream",
        "backend",
        "upstreams",
        "backends",
        "backend_upstreams",
        "balancing",
        "load_balance",
        "algorithm",
        "lb_algorithm",
        "load_balancing",
        "strategy",
        "target",
        "url",
        "address",
    ):
        fixed.pop(key, None)

    return fixed


def normalize_upstream_item(item: Any) -> dict[str, Any]:
    if isinstance(item, str):
        return {
            "address": normalize_upstream_address(item),
            "weight": 1,
        }

    if isinstance(item, dict):
        address = (
            item.get("address")
            or item.get("upstream")
            or item.get("backend")
            or item.get("target")
            or item.get("url")
            or "127.0.0.1:3000"
        )

        try:
            weight = int(item.get("weight", 1))
        except Exception:
            weight = 1

        if weight < 1:
            weight = 1

        return {
            "address": normalize_upstream_address(address),
            "weight": weight,
            **({"tls": bool(item["tls"])} if "tls" in item else {}),
            **({"server_name": str(item["server_name"])} if "server_name" in item else {}),
        }

    return {
        "address": "127.0.0.1:3000",
        "weight": 1,
    }


def normalize_route(route: dict[str, Any]) -> dict[str, Any]:
    if is_static_route(route):
        return normalize_static_route(route)

    path = normalize_path(
        route.get("path")
        or route.get("prefix")
        or route.get("route")
        or "/"
    )

    balancing = normalize_balancing(
        route.get("load_balance")
        or route.get("balancing")
        or route.get("load_balancing")
        or route.get("lb_algorithm")
        or route.get("algorithm")
        or route.get("strategy")
        or "round_robin"
    )

    upstreams_raw = route.get("upstreams")

    if upstreams_raw is None:
        if route.get("backends") is not None:
            upstreams_raw = route.get("backends")
        else:
            upstream = route.get("upstream") or route.get("backend") or "127.0.0.1:3000"
            upstreams_raw = [upstream]

    if not isinstance(upstreams_raw, list):
        upstreams_raw = [upstreams_raw]

    upstreams: list[dict[str, Any]] = []
    seen_addresses: set[str] = set()

    for item in upstreams_raw:
        upstream = normalize_upstream_item(item)
        address = upstream["address"]

        if address not in seen_addresses:
            upstreams.append(upstream)
            seen_addresses.add(address)
        else:
            for existing in upstreams:
                if existing["address"] == address:
                    existing["weight"] = max(
                        int(existing.get("weight", 1)),
                        int(upstream.get("weight", 1)),
                    )
                    break

    if not upstreams:
        upstreams = [
            {
                "address": "127.0.0.1:3000",
                "weight": 1,
            }
        ]

    if len(upstreams) > 1 and any(int(item.get("weight", 1)) != 1 for item in upstreams):
        balancing = "weighted_round_robin"

    fixed = dict(route)
    fixed["path"] = path
    fixed["balancing"] = balancing
    fixed["upstreams"] = upstreams
    fixed["upstream"] = upstreams[0]["address"]
    fixed["backend"] = upstreams[0]["address"]

    for key in (
        "load_balance",
        "algorithm",
        "lb_algorithm",
        "load_balancing",
        "strategy",
        "backends",
        "backend_upstreams",
        "address",
        "target",
        "url",
    ):
        fixed.pop(key, None)

    return fixed


def route_upstream_addresses(route: dict[str, Any]) -> list[str]:
    if is_static_route(route):
        return []

    normalized = normalize_route(route)
    addresses: list[str] = []

    for upstream in normalized.get("upstreams", []):
        if not isinstance(upstream, dict):
            upstream = normalize_upstream_item(upstream)

        address = normalize_upstream_address(
            upstream.get("address")
            or upstream.get("upstream")
            or upstream.get("backend")
            or "127.0.0.1:3000"
        )

        if address not in addresses:
            addresses.append(address)

    if not addresses and normalized.get("upstream"):
        addresses.append(normalize_upstream_address(normalized.get("upstream")))

    return addresses


def merge_routes_for_generation(routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered_paths: list[str] = []
    merged: dict[str, dict[str, Any]] = {}
    upstreams_by_path: dict[str, list[dict[str, Any]]] = {}

    for route in routes:
        if not isinstance(route, dict):
            continue

        normalized = normalize_route(route)
        path = normalized["path"]

        if path not in merged:
            merged[path] = normalized
            upstreams_by_path[path] = []
            ordered_paths.append(path)

        if normalized.get("type") == "static":
            merged[path] = normalized
            upstreams_by_path[path] = []
            continue

        if merged[path].get("type") == "static":
            continue

        if normalized.get("balancing"):
            merged[path]["balancing"] = normalized["balancing"]

        for upstream in normalized.get("upstreams", []):
            if not isinstance(upstream, dict):
                upstream = normalize_upstream_item(upstream)

            address = normalize_upstream_address(upstream.get("address"))
            weight = int(upstream.get("weight", 1) or 1)

            if weight < 1:
                weight = 1

            existing = next(
                (
                    item
                    for item in upstreams_by_path[path]
                    if item["address"] == address
                ),
                None,
            )

            if existing is None:
                upstreams_by_path[path].append(
                    {
                        "address": address,
                        "weight": weight,
                        **({"tls": upstream["tls"]} if "tls" in upstream else {}),
                        **({"server_name": upstream["server_name"]} if "server_name" in upstream else {}),
                    }
                )
            else:
                existing["weight"] = max(
                    int(existing.get("weight", 1)),
                    weight,
                )

    output: list[dict[str, Any]] = []

    for path in ordered_paths:
        route = dict(merged[path])

        if route.get("type") == "static":
            output.append(route)
            continue

        upstreams = upstreams_by_path[path]

        if not upstreams:
            upstreams = [
                {
                    "address": normalize_upstream_address(route.get("upstream")),
                    "weight": 1,
                }
            ]

        balancing = normalize_balancing(route.get("load_balance") or route.get("balancing"))

        if len(upstreams) > 1 and any(int(item.get("weight", 1)) != 1 for item in upstreams):
            balancing = "weighted_round_robin"

        route["path"] = path
        route["upstreams"] = upstreams
        route["upstream"] = upstreams[0]["address"]
        route["backend"] = upstreams[0]["address"]
        route["balancing"] = balancing

        for key in (
            "load_balance",
            "algorithm",
            "lb_algorithm",
            "load_balancing",
            "strategy",
            "backends",
            "backend_upstreams",
            "target",
            "url",
            "address",
        ):
            route.pop(key, None)

        output.append(route)

    return output


def collect_expected_upstream_addresses(config: dict[str, Any]) -> list[str]:
    addresses: list[str] = []

    routes = config.get("routes") or []

    if not isinstance(routes, list):
        return addresses

    for route in routes:
        if not isinstance(route, dict):
            continue

        for address in route_upstream_addresses(route):
            if address not in addresses:
                addresses.append(address)

    return addresses


def assert_rendered_rust_contains_upstreams(
    *,
    config: dict[str, Any],
    main_rs: str,
) -> None:
    missing = []

    for address in collect_expected_upstream_addresses(config):
        if address not in main_rs:
            missing.append(address)

    if missing:
        raise RuntimeError(
            "Generated Rust is missing upstream address(es): "
            + ", ".join(missing)
        )


def normalize_security(config: dict[str, Any]) -> dict[str, Any]:
    security = dict(DEFAULT_SECURITY)

    incoming = config.get("security")

    if isinstance(incoming, dict):
        security.update(incoming)

    blocked_paths = security.get("blocked_paths") or []

    if not isinstance(blocked_paths, list):
        blocked_paths = []

    security["blocked_paths"] = []

    for path in blocked_paths:
        if isinstance(path, str):
            fixed_path = normalize_path(path)

            if fixed_path not in security["blocked_paths"]:
                security["blocked_paths"].append(fixed_path)

    allowed_methods = security.get("allowed_methods") or []

    if not isinstance(allowed_methods, list):
        allowed_methods = []

    methods = []
    valid_methods = {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}

    for method in allowed_methods:
        if isinstance(method, str):
            upper = method.strip().upper()

            if upper in valid_methods and upper not in methods:
                methods.append(upper)

    security["allowed_methods"] = methods

    numeric_defaults = {
        "rate_limit_per_minute": 120,
        "max_connections": 1000,
        "max_request_body_bytes": 1048576,
        "upstream_timeout_seconds": 30,
    }

    for key, default in numeric_defaults.items():
        try:
            value = int(security.get(key, default))
        except Exception:
            value = default

        if value < 0:
            value = default

        security[key] = value

    return security


def normalize_config_for_generation(config: dict[str, Any]) -> dict[str, Any]:
    fixed = dict(config)

    fixed["port"] = normalize_port(
        fixed.get("port") or fixed.get("listen_port") or fixed.get("proxy_port"),
        default=9000,
    )

    routes = fixed.get("routes") or []
    normalized_routes: list[dict[str, Any]] = []

    if isinstance(routes, list):
        raw_routes = [route for route in routes if isinstance(route, dict)]
        normalized_routes = merge_routes_for_generation(raw_routes)

    if not normalized_routes:
        normalized_routes = [
            normalize_static_route(
                {
                    "path": "/",
                    "type": "static",
                    "root": "public",
                    "index": "index.html",
                }
            )
        ]

    fixed["routes"] = normalized_routes
    fixed["security"] = normalize_security(fixed)

    if "demo_backend" not in fixed:
        fixed["demo_backend"] = dict(DEFAULT_DEMO_BACKEND)

    return fixed


def route_path_to_dirname(route_path: str) -> str:
    route_path = normalize_path(route_path)

    if route_path == "/":
        return ""

    return route_path.strip("/")


def get_route_upstream(route: dict[str, Any]) -> str:
    addresses = route_upstream_addresses(route)

    if not addresses:
        return ""

    return addresses[0]


def get_route_upstreams_display(route: dict[str, Any]) -> str:
    if is_static_route(route):
        return f"static:{route.get('root', 'public')}/{route.get('index', 'index.html')}"

    addresses = route_upstream_addresses(route)

    if not addresses:
        return "127.0.0.1:3000"

    return ", ".join(addresses)


def route_algorithm_display(route: dict[str, Any]) -> str:
    if is_static_route(route):
        return "static webserver"

    algorithm = route.get("balancing")

    if not algorithm:
        return "single backend"

    return str(algorithm)


def route_upstreams_html(route: dict[str, Any]) -> str:
    if is_static_route(route):
        root = html.escape(str(route.get("root") or "public"))
        index = html.escape(str(route.get("index") or "index.html"))
        return f"<code>{root}/{index}</code>"

    upstreams = route.get("upstreams")

    if not isinstance(upstreams, list) or not upstreams:
        upstream = html.escape(str(route.get("upstream") or route.get("backend") or ""))
        return f"<code>{upstream}</code>"

    parts: list[str] = []

    for upstream in upstreams:
        if isinstance(upstream, dict):
            address = html.escape(str(upstream.get("address") or ""))
            weight = int(upstream.get("weight", 1) or 1)

            if weight != 1:
                parts.append(f"<code>{address}</code> <span class='weight'>weight {weight}</span>")
            else:
                parts.append(f"<code>{address}</code>")
        else:
            parts.append(f"<code>{html.escape(str(upstream))}</code>")

    return "<br>".join(parts)


def demo_backend_settings(config: dict[str, Any]) -> dict[str, Any]:
    incoming = config.get("demo_backend")

    if not isinstance(incoming, dict):
        return dict(DEFAULT_DEMO_BACKEND)

    settings = dict(DEFAULT_DEMO_BACKEND)
    settings.update(incoming)

    settings["enabled"] = bool(settings.get("enabled", True))
    settings["generate_placeholder_files"] = bool(
        settings.get("generate_placeholder_files", True)
    )
    settings["overwrite_existing"] = bool(settings.get("overwrite_existing", False))

    return settings


def render_live_route_directory_html(
    *,
    config: dict[str, Any],
    port: int,
    project_name: str = "AI Pingora Gateway",
) -> str:
    safe_project = html.escape(project_name)
    routes = config.get("routes") or []

    route_cards: list[str] = []

    if isinstance(routes, list):
        for route in routes:
            if not isinstance(route, dict):
                continue

            route_path = normalize_path(
                route.get("path")
                or route.get("prefix")
                or route.get("route")
                or "/"
            )

            href = route_path if route_path.endswith("/") else f"{route_path}/"
            full_url = f"http://127.0.0.1:{port}{href}"
            safe_route = html.escape(route_path)
            safe_href = html.escape(href)
            safe_full_url = html.escape(full_url)
            algorithm = html.escape(route_algorithm_display(route))
            upstreams = route_upstreams_html(route)

            if is_static_route(route):
                badge = "STATIC"
                badge_class = "static"
                description = "Served directly by the generated Pingora webserver."
            else:
                badge = "PROXY"
                badge_class = "proxy"
                description = "Forwarded by Pingora to backend service(s)."

            route_cards.append(
                f"""
                <section class="route-card">
                  <div class="route-header">
                    <div>
                      <span class="badge {badge_class}">{badge}</span>
                      <h2><a href="{safe_href}"><code>{safe_route}</code></a></h2>
                    </div>
                    <a class="open-link" href="{safe_href}">Open route →</a>
                  </div>

                  <p class="description">{html.escape(description)}</p>

                  <div class="meta-grid">
                    <div>
                      <span class="label">Live URL</span>
                      <code>{safe_full_url}</code>
                    </div>

                    <div>
                      <span class="label">Mode / Algorithm</span>
                      <code>{algorithm}</code>
                    </div>

                    <div>
                      <span class="label">Target</span>
                      <div class="upstreams">{upstreams}</div>
                    </div>

                    <div>
                      <span class="label">Test command</span>
                      <code>curl -i {safe_full_url}</code>
                    </div>
                  </div>
                </section>
                """
            )

    route_cards_html = "\n".join(route_cards) if route_cards else """
    <section class="route-card">
      <h2>No routes generated yet.</h2>
      <p>Create routes using your natural-language gateway prompt.</p>
    </section>
    """

    security = config.get("security") or {}
    blocked_paths = security.get("blocked_paths", [])
    allowed_methods = security.get("allowed_methods", [])

    blocked_html = ""
    if isinstance(blocked_paths, list) and blocked_paths:
        blocked_items = "\n".join(
            f"<code>{html.escape(str(path))}</code>"
            for path in blocked_paths
        )

        blocked_html = f"""
        <section class="security-card">
          <h2>Security blocked paths</h2>
          <div class="blocked-list">
            {blocked_items}
          </div>
        </section>
        """

    methods_html = ""
    if isinstance(allowed_methods, list) and allowed_methods:
        method_items = "\n".join(
            f"<code>{html.escape(str(method))}</code>"
            for method in allowed_methods
        )

        methods_html = f"""
        <section class="security-card">
          <h2>Allowed methods</h2>
          <div class="blocked-list">
            {method_items}
          </div>
        </section>
        """

    route_count = len(routes) if isinstance(routes, list) else 0

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{safe_project} - Route Directory</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root {{
      color-scheme: light;
      --bg: #f8fafc;
      --card: #ffffff;
      --border: #e5e7eb;
      --text: #0f172a;
      --muted: #64748b;
      --code-bg: #f1f5f9;
      --blue: #2563eb;
      --green: #047857;
      --purple: #7c3aed;
      --shadow: 0 10px 30px rgba(15, 23, 42, 0.08);
    }}

    * {{
      box-sizing: border-box;
    }}

    body {{
      margin: 0;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }}

    main {{
      max-width: 1180px;
      margin: 48px auto;
      padding: 0 24px 48px;
    }}

    .hero {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 24px;
      padding: 32px;
      box-shadow: var(--shadow);
      margin-bottom: 28px;
    }}

    .hero h1 {{
      margin: 0 0 10px;
      font-size: clamp(30px, 4vw, 46px);
      letter-spacing: -0.04em;
    }}

    .hero p {{
      margin: 8px 0;
      color: var(--muted);
      font-size: 18px;
    }}

    .summary {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin-top: 22px;
    }}

    .summary span {{
      background: var(--code-bg);
      border: 1px solid var(--border);
      padding: 8px 12px;
      border-radius: 999px;
      font-weight: 700;
      color: var(--text);
    }}

    .route-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
      gap: 20px;
    }}

    .route-card,
    .security-card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 22px;
      padding: 24px;
      box-shadow: var(--shadow);
    }}

    .route-header {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: flex-start;
    }}

    .route-card h2 {{
      margin: 10px 0 0;
      font-size: 26px;
    }}

    .route-card h2 a {{
      color: var(--text);
      text-decoration: none;
    }}

    .route-card h2 a:hover {{
      color: var(--blue);
    }}

    .open-link {{
      color: var(--blue);
      text-decoration: none;
      font-weight: 800;
      white-space: nowrap;
      margin-top: 4px;
    }}

    .open-link:hover {{
      text-decoration: underline;
    }}

    .badge {{
      display: inline-block;
      font-size: 12px;
      font-weight: 900;
      letter-spacing: 0.08em;
      padding: 5px 9px;
      border-radius: 999px;
      color: white;
    }}

    .badge.static {{
      background: var(--green);
    }}

    .badge.proxy {{
      background: var(--purple);
    }}

    .description {{
      color: var(--muted);
      margin: 14px 0 18px;
    }}

    .meta-grid {{
      display: grid;
      gap: 14px;
    }}

    .label {{
      display: block;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-weight: 900;
      color: var(--muted);
      margin-bottom: 4px;
    }}

    code {{
      display: inline-block;
      background: var(--code-bg);
      padding: 3px 7px;
      border-radius: 8px;
      color: var(--text);
      word-break: break-all;
    }}

    .upstreams {{
      display: flex;
      flex-direction: column;
      gap: 5px;
      align-items: flex-start;
    }}

    .weight {{
      color: var(--muted);
      font-weight: 700;
      margin-left: 6px;
    }}

    .security-card {{
      margin-top: 24px;
    }}

    .security-card h2 {{
      margin-top: 0;
    }}

    .blocked-list {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}

    footer {{
      color: var(--muted);
      margin-top: 28px;
      text-align: center;
      font-size: 14px;
    }}
  </style>
</head>
<body>
  <main>
    <section class="hero">
      <h1>✅ {safe_project} is running</h1>
      <p>This page is served directly by the generated Pingora webserver.</p>
      <p>Use this route directory to test every generated URL.</p>

      <div class="summary">
        <span>Live: <code>http://127.0.0.1:{port}/</code></span>
        <span>Routes: {route_count}</span>
        <span>Static + Proxy + Load Balancing</span>
      </div>
    </section>

    <section class="route-grid">
      {route_cards_html}
    </section>

    {blocked_html}
    {methods_html}

    <footer>
      Generated by AI Pingora Gateway route directory.
    </footer>
  </main>
</body>
</html>
"""


def render_demo_route_index_html(
    *,
    route_path: str,
    upstream: str,
    port: int,
    project_name: str = "AI Pingora Gateway",
) -> str:
    safe_route = html.escape(route_path)
    safe_upstream = html.escape(upstream)
    safe_project = html.escape(project_name)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{safe_project} - {safe_route}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body>
  <main>
    <h1>✅ Route works: <code>{safe_route}</code></h1>
    <p>This is demo backend content generated for local testing.</p>
    <p>Upstream(s): <code>{safe_upstream}</code></p>
    <p>Proxy: <code>http://127.0.0.1:{port}{safe_route}</code></p>
  </main>
</body>
</html>
"""


def render_demo_home_index_html(
    *,
    config: dict[str, Any],
    port: int,
    project_name: str = "AI Pingora Gateway",
) -> str:
    return render_live_route_directory_html(
        config=config,
        port=port,
        project_name=project_name,
    )


def write_text_if_allowed(path: Path, content: str, *, overwrite: bool) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists() and not overwrite:
        return False

    path.write_text(content, encoding="utf-8")
    return True


def write_json_if_allowed(path: Path, data: dict[str, Any], *, overwrite: bool) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists() and not overwrite:
        return False

    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    return True


def contained_path(project_path: Path, *parts: str) -> Path:
    base = project_path.resolve()
    target = base.joinpath(*parts).resolve()
    if not target.is_relative_to(base):
        raise ValueError("Generated file path escapes the project directory")
    return target


def write_default_public_files(project_path: Path, config: dict[str, Any]) -> None:
    routes = config.get("routes") or []

    if not any(isinstance(route, dict) and is_static_route(route) for route in routes):
        return

    port = int(config.get("port") or 8090)

    for route in routes:
        if not isinstance(route, dict) or not is_static_route(route):
            continue

        root = str(route.get("root") or "public")
        index = str(route.get("index") or "index.html")

        public_dir = contained_path(project_path, root)
        index_path = contained_path(project_path, root, index)
        public_dir.mkdir(parents=True, exist_ok=True)

        # Always refresh homepage so it shows the latest route directory.
        index_path.write_text(
            render_live_route_directory_html(
                config=config,
                port=port,
                project_name="AI Pingora Gateway",
            ),
            encoding="utf-8",
        )


def write_demo_backend_files(
    config: dict[str, Any],
    project_dir: str | Path,
) -> dict[str, Any]:
    project_path = Path(project_dir).resolve()
    settings = demo_backend_settings(config)

    result: dict[str, Any] = {
        "enabled": settings["enabled"],
        "project_dir": str(project_path),
        "created": [],
        "skipped": [],
    }

    if not settings["enabled"]:
        return result

    if not settings["generate_placeholder_files"]:
        return result

    overwrite = settings["overwrite_existing"]

    routes = config.get("routes") or []
    port = int(config.get("port") or config.get("listen_port") or config.get("proxy_port") or 9000)

    if not isinstance(routes, list):
        return result

    for route in routes:
        if not isinstance(route, dict):
            continue

        if is_static_route(route):
            continue

        route_path = normalize_path(route.get("path") or route.get("prefix") or route.get("route"))
        upstream = get_route_upstreams_display(route)

        dirname = route_path_to_dirname(route_path)
        target_dir = contained_path(project_path, dirname)

        index_path = contained_path(project_path, dirname, "index.html")
        metadata_path = contained_path(project_path, dirname, "route.json")

        if route_path == "/":
            index_html = render_demo_home_index_html(
                config=config,
                port=port,
            )
        else:
            index_html = render_demo_route_index_html(
                route_path=route_path,
                upstream=upstream,
                port=port,
            )

        metadata = {
            "route": route_path,
            "upstream": get_route_upstream(route),
            "upstreams": route_upstream_addresses(route),
            "balancing": normalize_balancing(route.get("balancing")),
            "generated_by": "AI Pingora Gateway demo backend writer",
            "note": "This file is safe to replace with your own backend/static content.",
            "route_config": route,
        }

        wrote_index = write_text_if_allowed(index_path, index_html, overwrite=overwrite)
        wrote_metadata = write_json_if_allowed(metadata_path, metadata, overwrite=overwrite)

        if wrote_index or wrote_metadata:
            result["created"].append(
                {
                    "route": route_path,
                    "dir": str(target_dir),
                    "index": str(index_path),
                    "metadata": str(metadata_path),
                }
            )
        else:
            result["skipped"].append(
                {
                    "route": route_path,
                    "dir": str(target_dir),
                    "reason": "files already exist",
                }
            )

    return result


def render_cargo_toml() -> str:
    return """[package]
name = "generated-pingora-proxy"
version = "0.2.0"
license = "Apache-2.0"
edition = "2021"

[dependencies]
async-trait = "0.1"
bytes = "1"
pingora = { version = "=0.9.1", features = ["proxy", "rustls"] }
tokio = { version = "1", features = ["fs", "net"] }
serde_json = "1"
serde = { version = "1", features = ["derive"] }
ipnet = "2"
subtle = "=2.6.1"
rustls-pki-types = { version = "1", features = ["std"] }
rustls = { version = "0.23.46", default-features = false, features = ["ring", "std", "tls12"] }
x509-parser = "=0.16.0"
"""


def render_route_configs(config: dict[str, Any]) -> str:
    routes = config["routes"]
    rendered_routes = []

    for route in routes:
        normalized_route = normalize_route(route)

        route_type = str(normalized_route.get("type") or "proxy").lower()
        root = str(normalized_route.get("root") or "public")
        index = str(normalized_route.get("index") or "index.html")

        upstreams = normalized_route.get("upstreams") or []

        rendered_upstreams = []

        for upstream in upstreams:
            if not isinstance(upstream, dict):
                upstream = normalize_upstream_item(upstream)

            address = normalize_upstream_address(upstream.get("address"))
            weight = int(upstream.get("weight", 1) or 1)

            if weight < 1:
                weight = 1

            rendered_upstreams.append(
                f"""UpstreamConfig {{
            address: {rust_string(address)}.to_string(),
            weight: {weight},
            tls: {str(bool(upstream.get("tls", False))).lower()},
            server_name: {rust_string(upstream.get("server_name", ""))}.to_string(),
        }}"""
            )

        upstreams_rs = ",\n        ".join(rendered_upstreams)

        rendered_routes.append(
            f"""RouteConfig {{
        path: {rust_string(normalized_route["path"])}.to_string(),
        route_type: {rust_string(route_type)}.to_string(),
        root: {rust_string(root)}.to_string(),
        index: {rust_string(index)}.to_string(),
        balancing: {rust_string(normalized_route.get("balancing", "round_robin"))}.to_string(),
        upstreams: vec![
        {upstreams_rs}
        ],
    }}"""
        )

    return ",\n    ".join(rendered_routes)


def render_security_config(config: dict[str, Any]) -> str:
    security = config["security"]

    blocked_paths = ", ".join(
        f"{rust_string(path)}.to_string()" for path in security.get("blocked_paths", [])
    )

    allowed_methods = ", ".join(
        f"{rust_string(method)}.to_string()" for method in security.get("allowed_methods", [])
    )

    return f"""SecurityConfig {{
        blocked_paths: vec![{blocked_paths}],
        allowed_methods: vec![{allowed_methods}],
        rate_limit_per_minute: {int(security.get("rate_limit_per_minute", 120))},
        max_connections: {int(security.get("max_connections", 1000))},
        max_request_body_bytes: {int(security.get("max_request_body_bytes", 1048576))},
        upstream_timeout_seconds: {int(security.get("upstream_timeout_seconds", 30))},
        require_api_key: {str(bool(security.get("require_api_key", False))).lower()},
        failure_threshold: {max(1, int(security.get("failure_threshold", 3)))},
        failure_cooldown_seconds: {max(1, int(security.get("failure_cooldown_seconds", 10)))},
    }}"""


def render_main_rs(
    config: dict[str, Any],
    routes_override: list[dict[str, Any]] | None = None,
) -> str:
    config = normalize_config_for_generation(config)

    if routes_override is not None:
        override_config = dict(config)
        override_config["routes"] = routes_override
        config = normalize_config_for_generation(override_config)

    port = int(config["port"])
    routes_rs = render_route_configs(config)
    security_rs = render_security_config(config)

    source = f"""use async_trait::async_trait;
use bytes::Bytes;
use subtle::ConstantTimeEq;
use pingora::http::ResponseHeader;
use pingora::prelude::*;
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::atomic::{{AtomicUsize, Ordering}};
use std::sync::{{Arc, Mutex}};
use std::time::{{Duration, Instant, SystemTime, UNIX_EPOCH}};

#[derive(Clone, Debug, serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct UpstreamConfig {{
    address: String,
    #[serde(default = "default_weight")]
    weight: usize,
    #[serde(default)]
    tls: bool,
    #[serde(default)]
    server_name: String,
}}

#[derive(Clone, Debug)]
struct RouteConfig {{
    path: String,
    route_type: String,
    root: String,
    index: String,
    balancing: String,
    upstreams: Vec<UpstreamConfig>,
}}

#[derive(Clone, Debug, serde::Deserialize)]
#[serde(default, deny_unknown_fields)]
struct SecurityConfig {{
    blocked_paths: Vec<String>,
    allowed_methods: Vec<String>,
    rate_limit_per_minute: usize,
    max_connections: usize,
    max_request_body_bytes: usize,
    upstream_timeout_seconds: u64,
    require_api_key: bool,
    failure_threshold: usize,
    failure_cooldown_seconds: u64,
}}

#[derive(Debug)]
struct RateBucket {{
    window_start: Instant,
    count: usize,
}}

#[derive(Debug, Default)]
struct BackendHealth {{
    failures: usize,
    until: Option<Instant>,
    generation: usize,
}}

#[derive(Debug)]
struct RequestContext {{
    body_bytes: usize,
    active: Option<Arc<AtomicUsize>>,
    upstream: Option<Arc<AtomicUsize>>,
    started: Instant,
    selected: Option<(usize, usize, usize)>,
    dns_failed: bool,
    snapshot: Option<Arc<Runtime>>,
    consumer: Option<String>,
}}

impl Drop for RequestContext {{
    fn drop(&mut self) {{
        if let Some(counter) = self.active.take() {{ counter.fetch_sub(1, Ordering::Relaxed); }}
        if let Some(counter) = self.upstream.take() {{ counter.fetch_sub(1, Ordering::Relaxed); }}
    }}
}}

#[derive(Clone, Debug)]
struct Runtime {{
    routes: Arc<Vec<RouteConfig>>,
    counters: Arc<Vec<AtomicUsize>>,
    upstream_loads: Arc<Vec<Vec<Arc<AtomicUsize>>>>,
    active: Arc<AtomicUsize>,
    security: SecurityConfig,
    rate_limiter: Arc<Mutex<HashMap<String, RateBucket>>>,
    health: Arc<Mutex<Vec<Vec<BackendHealth>>>>,
    api_keys: Arc<Vec<ApiCredential>>,
    client_identity: Option<Arc<pingora::utils::tls::CertKey>>,
    completed: Arc<AtomicUsize>,
    failed: Arc<AtomicUsize>,
}}

fn content_type_for_path(path: &PathBuf) -> &'static str {{
    match path.extension().and_then(|value| value.to_str()).unwrap_or("") {{
        "html" | "htm" => "text/html; charset=utf-8",
        "css" => "text/css; charset=utf-8",
        "js" => "application/javascript; charset=utf-8",
        "json" => "application/json; charset=utf-8",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "svg" => "image/svg+xml",
        "txt" => "text/plain; charset=utf-8",
        "ico" => "image/x-icon",
        _ => "application/octet-stream",
    }}
}}

fn safe_static_path(root: &str, relative_path: &str, index: &str) -> Option<PathBuf> {{
    let root_path = std::fs::canonicalize(root).ok()?;
    let mut path = root_path.clone();
    let clean_relative = relative_path.trim_start_matches('/');
    for part in clean_relative.split('/') {{
        if part.is_empty() {{ continue; }}
        if part.starts_with('.') || part.contains('\\\\') || part.contains('%') {{ return None; }}
        path.push(part);
    }}
    if clean_relative.is_empty() || relative_path.ends_with('/') {{ path.push(index); }}
    let canonical = std::fs::canonicalize(path).ok()?;
    if !canonical.starts_with(&root_path) || !canonical.is_file() {{ return None; }}
    Some(canonical)
}}

impl Runtime {{
    fn new(routes: Vec<RouteConfig>, security: SecurityConfig) -> Self {{
        let keys = load_api_keys(security.require_api_key).expect("invalid API key configuration");
        let mut runtime = Self::with_keys(routes, security, keys);
        runtime.client_identity = load_client_identity().expect("invalid client TLS identity");
        runtime
    }}
    fn with_keys(routes: Vec<RouteConfig>, security: SecurityConfig, api_keys: Vec<ApiCredential>) -> Self {{
        let counters = routes
            .iter()
            .map(|_| AtomicUsize::new(0))
            .collect::<Vec<_>>();

        let upstream_loads = routes
            .iter()
            .map(|route| {{
                route
                    .upstreams
                    .iter()
                    .map(|_| Arc::new(AtomicUsize::new(0)))
                    .collect::<Vec<_>>()
            }})
            .collect::<Vec<_>>();

        let health = routes.iter().map(|r| r.upstreams.iter()
            .map(|_| BackendHealth::default()).collect()).collect();
        Self {{
            health: Arc::new(Mutex::new(health)),
            api_keys: Arc::new(api_keys),
            client_identity: None,
            completed: Arc::new(AtomicUsize::new(0)),
            failed: Arc::new(AtomicUsize::new(0)),
            routes: Arc::new(routes),
            counters: Arc::new(counters),
            upstream_loads: Arc::new(upstream_loads),
            active: Arc::new(AtomicUsize::new(0)),
            security,
            rate_limiter: Arc::new(Mutex::new(HashMap::new())),
        }}
    }}

    fn match_route_index(&self, path: &str) -> Option<usize> {{
        let mut best_match: Option<(usize, usize)> = None;

        for (index, route) in self.routes.iter().enumerate() {{
            let route_path = route.path.as_str();

            let matched = if route_path == "/" {{
                true
            }} else {{
                path == route_path || path.starts_with(&format!("{{}}/", route_path.trim_end_matches('/')))
            }};

            if matched {{
                let score = route_path.len();

                match best_match {{
                    Some((_, best_score)) if best_score >= score => {{}}
                    _ => best_match = Some((index, score)),
                }}
            }}
        }}

        best_match.map(|(index, _)| index)
    }}

    async fn serve_static_route(
        &self,
        session: &mut Session,
        route: &RouteConfig,
        request_path: &str,
    ) -> Result<bool> {{
        let relative_path = if route.path == "/" {{
            request_path
        }} else {{
            request_path
                .strip_prefix(route.path.trim_end_matches('/'))
                .unwrap_or("")
        }};

        let file_path = match safe_static_path(&route.root, relative_path, &route.index) {{
            Some(path) => path,
            None => {{
                session.respond_error(403).await?;
                return Ok(true);
            }}
        }};

        // Static roots must be immutable at runtime (the container is read-only).
        if tokio::fs::metadata(&file_path).await.map(|m| m.len()).unwrap_or(u64::MAX) > 8 * 1024 * 1024 {{
            session.respond_error(413).await?;
            return Ok(true);
        }}
        let body = match tokio::fs::read(&file_path).await {{
            Ok(value) => value,
            Err(_) => {{
                session.respond_error(404).await?;
                return Ok(true);
            }}
        }};

        let mut header = ResponseHeader::build(200, None)?;
        header.insert_header("content-type", content_type_for_path(&file_path))?;
        header.insert_header("content-length", body.len().to_string())?;

        session.write_response_header(Box::new(header), false).await?;

        if session.req_header().method.as_str().eq_ignore_ascii_case("HEAD") {{
            session.write_response_body(None, true).await?;
        }} else {{
            session
                .write_response_body(Some(Bytes::from(body)), true)
                .await?;
        }}

        Ok(true)
    }}

    fn select_upstream(&self, route_index: usize, session: &Session, ctx: &mut RequestContext) -> Option<UpstreamConfig> {{
        let route = self.routes.get(route_index)?;

        if route.upstreams.is_empty() {{
            return None;
        }}

        let mut health = self.health.lock().ok()?;
        let states = health.get_mut(route_index)?;
        let now = Instant::now();
        for state in states.iter_mut() {{
            if state.until.is_some_and(|until| now >= until) {{
                state.until = None;
                state.failures = 0;
                state.generation = state.generation.wrapping_add(1);
            }}
        }}
        let eligible: Vec<usize> = states.iter().enumerate()
            .filter(|(_, state)| state.until.is_none()).map(|(i, _)| i).collect();
        if eligible.is_empty() {{ return None; }}
        let len = eligible.len();
        let index = match route.balancing.as_str() {{
            "random" => self.select_random_index(route_index, len),
            "weighted_round_robin" => {{
                let mut available = route.clone();
                available.upstreams = eligible.iter().map(|i| route.upstreams[*i].clone()).collect();
                self.select_weighted_round_robin_index(route_index, &available)
            }},
            "least_connections" => {{
                let start = self.select_round_robin_index(route_index, len);
                (0..len).map(|offset| (start + offset) % len)
                    .min_by_key(|i| self.upstream_loads[route_index][eligible[*i]].load(Ordering::Relaxed)).unwrap()
            }},
            "ip_hash" => self.select_ip_hash_index(session, len),
            _ => self.select_round_robin_index(route_index, len),
        }};
        let selected = eligible[index];
        ctx.selected = Some((route_index, selected, states[selected].generation));

        if let Some(previous) = ctx.upstream.take() {{ previous.fetch_sub(1, Ordering::Relaxed); }}
        let counter = self.upstream_loads[route_index][selected].clone();
        counter.fetch_add(1, Ordering::Relaxed);
        ctx.upstream = Some(counter);
        Some(route.upstreams[selected].clone())
    }}

    fn select_round_robin_index(&self, route_index: usize, len: usize) -> usize {{
        let counter = self.counters[route_index].fetch_add(1, Ordering::Relaxed);
        counter % len
    }}

    fn select_random_index(&self, route_index: usize, len: usize) -> usize {{
        let counter = self.counters[route_index].fetch_add(1, Ordering::Relaxed);

        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_else(|_| Duration::from_secs(0))
            .subsec_nanos() as usize;

        let mixed = counter
            .wrapping_mul(1_103_515_245usize)
            .wrapping_add(12_345usize)
            .wrapping_add(nanos)
            .wrapping_add(route_index.wrapping_mul(2_654_435_761usize));

        mixed % len
    }}

    fn select_weighted_round_robin_index(&self, route_index: usize, route: &RouteConfig) -> usize {{
        let total_weight: usize = route
            .upstreams
            .iter()
            .map(|upstream| upstream.weight.max(1))
            .sum();

        if total_weight == 0 {{
            return self.select_round_robin_index(route_index, route.upstreams.len());
        }}

        let counter = self.counters[route_index].fetch_add(1, Ordering::Relaxed);
        let mut ticket = counter % total_weight;

        for (index, upstream) in route.upstreams.iter().enumerate() {{
            let weight = upstream.weight.max(1);

            if ticket < weight {{
                return index;
            }}

            ticket -= weight;
        }}

        0
    }}

    fn select_ip_hash_index(&self, session: &Session, len: usize) -> usize {{
        let key = client_key(session);
        let mut hash: usize = 2_166_136_261usize;

        for byte in key.as_bytes() {{
            hash ^= *byte as usize;
            hash = hash.wrapping_mul(16_777_619usize);
        }}

        hash % len
    }}

    fn is_blocked_path(&self, path: &str) -> bool {{
        for blocked in &self.security.blocked_paths {{
            if path == blocked || path.starts_with(&format!("{{}}/", blocked.trim_end_matches('/'))) {{
                return true;
            }}
        }}

        false
    }}

    fn method_allowed(&self, method: &str) -> bool {{
        if self.security.allowed_methods.is_empty() {{
            return true;
        }}

        self.security
            .allowed_methods
            .iter()
            .any(|allowed| allowed.eq_ignore_ascii_case(method))
    }}

    fn check_rate_limit(&self, client_key: &str) -> bool {{
        self.check_quota(client_key, self.security.rate_limit_per_minute)
    }}
    fn check_quota(&self, client_key: &str, limit: usize) -> bool {{

        if limit == 0 {{
            return true;
        }}

        let mut limiter = match self.rate_limiter.lock() {{
            Ok(guard) => guard,
            Err(_) => return false,
        }};

        let now = Instant::now();

        if limiter.len() >= 10000 {{
            limiter.retain(|_, bucket| now.duration_since(bucket.window_start) < Duration::from_secs(60));
            if limiter.len() >= 10000 && !limiter.contains_key(client_key) {{ return false; }}
        }}
        let bucket = limiter.entry(client_key.to_string()).or_insert(RateBucket {{
            window_start: now,
            count: 0,
        }});

        if now.duration_since(bucket.window_start) >= Duration::from_secs(60) {{
            bucket.window_start = now;
            bucket.count = 0;
        }}

        if bucket.count >= limit {{
            return false;
        }}

        bucket.count += 1;
        true
    }}
}}

fn parse_upstream(upstream: &str) -> (String, u16) {{
    let cleaned = upstream
        .trim()
        .trim_start_matches("http://")
        .trim_start_matches("https://")
        .trim_end_matches('/');

    let mut parts = cleaned.rsplitn(2, ':');

    let port_str = parts.next().unwrap_or("80");
    let host = parts.next().unwrap_or(cleaned);

    let port = port_str.parse::<u16>().unwrap_or(80);

    (host.to_string(), port)
}}

fn load_api_keys(required: bool) -> std::result::Result<Vec<ApiCredential>, String> {{
    if !required {{ return Ok(Vec::new()); }}
    let path = std::env::var("PINGOX_API_KEYS_FILE").map_err(|_| "PINGOX_API_KEYS_FILE is required")?;
    let file = std::fs::File::open(path).map_err(|_| "cannot open API key file")?;
    use std::io::Read;
    let mut bytes = Vec::new();
    file.take(16385).read_to_end(&mut bytes).map_err(|_| "cannot read API key file")?;
    if bytes.len() > 16384 {{ return Err("API key file too large".into()); }}
    parse_credentials(&bytes)
}}

fn trusted_proxy(session: &Session) -> bool {{
    static NETWORKS: std::sync::OnceLock<Vec<ipnet::IpNet>> = std::sync::OnceLock::new();
    let networks = NETWORKS.get_or_init(|| {{
        std::env::var("TRUSTED_PROXY_CIDRS").unwrap_or_default().split(',')
            .filter(|s| !s.trim().is_empty()).map(|s| s.trim().parse().expect("invalid TRUSTED_PROXY_CIDRS")).collect()
    }});
    session.client_addr().and_then(|a| a.as_inet())
        .map(|a| networks.iter().any(|n| n.contains(&a.ip()))).unwrap_or(false)
}}

fn client_key(session: &Session) -> String {{
    if trusted_proxy(session) {{
        if let Some(ip) = session.req_header().headers.get("x-real-ip")
            .and_then(|h| h.to_str().ok()).and_then(|s| s.parse::<std::net::IpAddr>().ok()) {{
            return ip.to_string();
        }}
    }}
    session.client_addr().and_then(|a| a.as_inet()).map(|a| a.ip().to_string())
        .unwrap_or_else(|| "unknown".to_string())
}}

#[async_trait]
impl ProxyHttp for Runtime {{
    type CTX = RequestContext;

    fn new_ctx(&self) -> Self::CTX {{
        RequestContext {{ body_bytes: 0, active: None, upstream: None, started: Instant::now(), selected: None, dns_failed: false, snapshot: None, consumer: None }}
    }}

    async fn request_filter(
        &self,
        session: &mut Session,
        ctx: &mut Self::CTX,
    ) -> Result<bool> {{
        let path = session.req_header().uri.path().to_string();
        let method = session.req_header().method.as_str().to_uppercase();

        // Reject ambiguous encodings instead of enforcing policy on one path and
        // forwarding another interpretation to an upstream.
        if path.contains('%') || path.contains('\\\\') || path.contains("//")
            || path.split('/').any(|part| part == "." || part == "..") {{
            session.respond_error(400).await?;
            return Ok(true);
        }}
        if path == "/_gateway/health" && (method == "GET" || method == "HEAD") {{
            let mut header = ResponseHeader::build(200, None)?;
            header.insert_header("content-length", "0")?;
            session.write_response_header(Box::new(header), true).await?;
            return Ok(true);
        }}
        if (path == "/_gateway/ready" || path == "/_gateway/metrics") && (method == "GET" || method == "HEAD") {{
            let (unavailable, ready) = {{
            let health = self.health.lock().expect("health state");
            let now = Instant::now();
            let unavailable = health.iter().flatten().filter(|s| s.until.is_some_and(|t| t > now)).count();
            let ready = health.iter().all(|pool| pool.is_empty() || pool.iter().any(|s| s.until.is_none_or(|t| t <= now)));
            (unavailable, ready)
            }};
            let body = if path.ends_with("metrics") {{ format!(
                "# TYPE pingox_requests_completed_total counter\npingox_requests_completed_total {{}}\n# TYPE pingox_requests_failed_total counter\npingox_requests_failed_total {{}}\n# TYPE pingox_active_requests gauge\npingox_active_requests {{}}\n# TYPE pingox_unavailable_backends gauge\npingox_unavailable_backends {{}}\n",
                self.completed.load(Ordering::Relaxed), self.failed.load(Ordering::Relaxed),
                self.active.load(Ordering::Relaxed), unavailable) }} else {{ String::new() }};
            let status = if path.ends_with("ready") && !ready {{ 503 }} else {{ 200 }};
            let mut header = ResponseHeader::build(status, None)?;
            header.insert_header("content-type", "text/plain; version=0.0.4")?;
            header.insert_header("content-length", body.len().to_string())?;
            session.write_response_header(Box::new(header), method == "HEAD" || body.is_empty()).await?;
            if method != "HEAD" && !body.is_empty() {{ session.write_response_body(Some(Bytes::from(body)), true).await?; }}
            return Ok(true);
        }}
        if path == "/_gateway" || path.starts_with("/_gateway/") {{
            session.respond_error(404).await?;
            return Ok(true);
        }}
        if self.active.fetch_update(Ordering::Relaxed, Ordering::Relaxed, |count| {{
            if count < self.security.max_connections {{ Some(count + 1) }} else {{ None }}
        }}).is_err() {{
            session.respond_error(503).await?;
            return Ok(true);
        }}
        ctx.active = Some(self.active.clone());
        session.set_read_timeout(Some(Duration::from_secs(self.security.upstream_timeout_seconds)));
        if !self.method_allowed(&method) {{
            session.respond_error(405).await?;
            return Ok(true);
        }}

        if self.is_blocked_path(&path) {{
            session.respond_error(403).await?;
            return Ok(true);
        }}

        if session.req_header().headers.get("upgrade").is_some() {{
            session.respond_error(400).await?;
            return Ok(true);
        }}

        if let Some(value) = session.req_header().headers.get("content-length") {{
            if let Ok(text) = value.to_str() {{
                if let Ok(length) = text.parse::<usize>() {{
                    if length > self.security.max_request_body_bytes {{
                        session.respond_error(413).await?;
                        return Ok(true);
                    }}
                }}
            }}
        }}

        let key = client_key(session);

        if !self.check_rate_limit(&key) {{
            session.respond_error(429).await?;
            return Ok(true);
        }}

        if self.security.require_api_key {{
            let values = session.req_header().headers.get_all("x-pingox-api-key");
            let mut values = values.iter();
            let supplied = values.next().map(|h| h.as_bytes()).unwrap_or(&[]);
            let duplicate = values.next().is_some();
            let mut valid = subtle::Choice::from(0);
            let mut matched = None;
            for (index, credential) in self.api_keys.iter().enumerate() {{
                let equal = credential.key.as_slice().ct_eq(supplied);
                valid |= equal;
                if bool::from(equal) {{ matched = Some(index); }}
            }}
            if duplicate || !bool::from(valid) {{
                session.respond_error(401).await?;
                return Ok(true);
            }}
            let credential = &self.api_keys[matched.expect("matched credential")];
            ctx.consumer = Some(credential.id.clone());
            if !credential.methods.contains(&method) || !credential.routes.iter().any(|prefix|
                prefix == "/" || path == *prefix || path.starts_with(&format!("{{}}/", prefix))) {{
                session.respond_error(403).await?;
                return Ok(true);
            }}
            if !self.check_quota(&format!("consumer:{{}}", credential.id), credential.quota) {{
                session.respond_error(429).await?;
                return Ok(true);
            }}
        }}

        let route_index = match self.match_route_index(&path) {{
            Some(index) => index,
            None => {{
                session.respond_error(404).await?;
                return Ok(true);
            }}
        }};

        let route = match self.routes.get(route_index) {{
            Some(value) => value.clone(),
            None => {{
                session.respond_error(404).await?;
                return Ok(true);
            }}
        }};

        if route.route_type == "static" {{
            return self.serve_static_route(session, &route, &path).await;
        }}

        Ok(false)
    }}

    async fn upstream_request_filter(
        &self, session: &mut Session, request: &mut pingora::http::RequestHeader,
        ctx: &mut Self::CTX,
    ) -> Result<()> {{
        if let Some((route, backend, _)) = ctx.selected {{
            let upstream = &self.routes[route].upstreams[backend];
            if upstream.tls {{ request.insert_header("host", upstream.server_name.clone())?; }}
        }}
        let ip = client_key(session);
        let proto = if trusted_proxy(session) && session.req_header().headers.get("x-forwarded-proto")
            .and_then(|h| h.to_str().ok()) == Some("https") {{ "https" }} else {{ "http" }};
        request.remove_header("x-pingox-api-key");
        request.remove_header("forwarded");
        request.insert_header("x-forwarded-for", ip.clone())?;
        request.insert_header("x-real-ip", ip)?;
        request.insert_header("x-forwarded-proto", proto)?;
        Ok(())
    }}

    async fn request_body_filter(
        &self, _session: &mut Session, body: &mut Option<Bytes>,
        _end_of_stream: bool, ctx: &mut Self::CTX,
    ) -> Result<()> {{
        ctx.body_bytes = ctx.body_bytes.saturating_add(body.as_ref().map_or(0, |b| b.len()));
        if ctx.body_bytes > self.security.max_request_body_bytes {{
            return Err(Error::new(ErrorType::HTTPStatus(413)));
        }}
        Ok(())
    }}

    async fn logging(&self, session: &mut Session, error: Option<&Error>, ctx: &mut Self::CTX) {{
        if let Some(counter) = ctx.active.take() {{ counter.fetch_sub(1, Ordering::Relaxed); }}
        if let Some(counter) = ctx.upstream.take() {{ counter.fetch_sub(1, Ordering::Relaxed); }}
        let status = session.response_written().map(|r| r.status.as_u16()).unwrap_or(0);
        let internal = session.req_header().uri.path().starts_with("/_gateway/");
        if !internal {{
            self.completed.fetch_add(1, Ordering::Relaxed);
            if error.is_some() || status >= 500 {{ self.failed.fetch_add(1, Ordering::Relaxed); }}
        }}
        if let Some((route, backend, generation)) = ctx.selected.take() {{
            let failure = ctx.dns_failed || error.is_some_and(|e| *e.esource() == pingora::ErrorSource::Upstream)
                || (error.is_none() && status >= 500);
            let success = error.is_none() && (200..500).contains(&status);
            if let Ok(mut health) = self.health.lock() {{
                let state = &mut health[route][backend];
                if state.generation == generation && state.until.is_none() {{
                    if failure {{
                        state.failures = state.failures.saturating_add(1);
                        if state.failures >= self.security.failure_threshold {{
                            state.until = Some(Instant::now() + Duration::from_secs(self.security.failure_cooldown_seconds));
                            state.generation = state.generation.wrapping_add(1);
                        }}
                    }} else if success {{ state.failures = 0; }}
                }}
            }}
        }}
        // Do not log credentials, query strings, bodies, or backend error payloads.
        println!("{{}}", serde_json::json!({{
            "event": "request", "consumer": ctx.consumer, "method": session.req_header().method.as_str(),
            "status": session.response_written().map(|r| r.status.as_u16()),
            "duration_ms": ctx.started.elapsed().as_millis(), "failed": error.is_some(),
        }}));
    }}

    async fn upstream_peer(
        &self,
        session: &mut Session,
        ctx: &mut Self::CTX,
    ) -> Result<Box<HttpPeer>> {{
        let path = session.req_header().uri.path();

        let route_index = self.match_route_index(path).ok_or_else(|| Error::new(ErrorType::HTTPStatus(404)))?;

        let upstream = self
            .select_upstream(route_index, session, ctx)
            .ok_or_else(|| Error::new(ErrorType::HTTPStatus(503)))?;

        let (host, port) = parse_upstream(upstream.address.as_str());

        // Async DNS avoids blocking worker threads and HttpPeer's resolving constructor.
        let address = tokio::time::timeout(Duration::from_secs(self.security.upstream_timeout_seconds),
            tokio::net::lookup_host((host.as_str(), port))).await
            .map_err(|_| {{ ctx.dns_failed = true; Error::new(ErrorType::HTTPStatus(504)) }})?
            .map_err(|_| {{ ctx.dns_failed = true; Error::new(ErrorType::HTTPStatus(502)) }})?
            .next().ok_or_else(|| {{ ctx.dns_failed = true; Error::new(ErrorType::HTTPStatus(502)) }})?;
        let mut peer = Box::new(HttpPeer::new(address, upstream.tls, upstream.server_name.clone()));
        if upstream.tls {{ peer.client_cert_key = self.client_identity.clone(); }}
        let timeout = Some(Duration::from_secs(self.security.upstream_timeout_seconds));
        peer.options.connection_timeout = timeout;
        peer.options.total_connection_timeout = timeout;
        peer.options.read_timeout = timeout;
        peer.options.write_timeout = timeout;
        Ok(peer)
    }}
}}

fn build_routes() -> Vec<RouteConfig> {{
    vec![
    {routes_rs}
    ]
}}

fn build_security() -> SecurityConfig {{
    {security_rs}
}}


"""

    return source + Path(__file__).with_name("runtime_control.rs").read_text().replace("__PORT__", str(port))


def write_project(
    config: dict[str, Any],
    project_dir: str | Path | None = None,
) -> Path:
    project_path = Path(project_dir) if project_dir is not None else PROJECT_DIR
    project_path = project_path.resolve()

    src_dir = project_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    normalized_config = normalize_config_for_generation(config)

    write_default_public_files(project_path, normalized_config)

    cargo_toml = render_cargo_toml()
    main_rs = render_main_rs(normalized_config)

    assert_rendered_rust_contains_upstreams(
        config=normalized_config,
        main_rs=main_rs,
    )

    (project_path / "Cargo.toml").write_text(cargo_toml, encoding="utf-8")
    (src_dir / "main.rs").write_text(main_rs, encoding="utf-8")

    with (project_path / "config.json").open("w", encoding="utf-8") as f:
        json.dump(normalized_config, f, indent=2)

    try:
        demo_result = write_demo_backend_files(normalized_config, project_path)

        if demo_result.get("enabled"):
            created = demo_result.get("created", [])
            skipped = demo_result.get("skipped", [])

            if created:
                print(f"✅ Demo backend files created: {len(created)} route file set(s)")

            if skipped:
                print(f"ℹ️ Demo backend files skipped: {len(skipped)} existing route file set(s)")
    except Exception as exc:
        print(f"⚠️ Demo backend file generation skipped: {exc}")

    print("✅ Project generated successfully")
    print(f"📁 Folder: {project_path}")

    return project_path