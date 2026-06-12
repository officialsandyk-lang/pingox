from __future__ import annotations
from core.homepage_route import ensure_route_directory_homepage

import html
import json
import re
import shutil
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROJECT_DIR = PROJECT_ROOT / "generated-pingora-proxy"

# Compatibility alias used by older runner/graph code.
PROJECT_DIR = DEFAULT_PROJECT_DIR
DEFAULT_RUNTIME_TEMPLATE_DIR = PROJECT_ROOT / "templates" / "pingora_runtime"


COPY_EXCLUDE_NAMES = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    "target",
    ".runtime",
    "runtime",
    "tmp",
    "data",
}

COPY_EXCLUDE_FILES = {
    "config.json",
    "active_config.json",
    "gateway_config.json",
    "logs.jsonl",
}


def normalize_path(path: Any) -> str:
    text = str(path or "/").strip()

    if not text:
        return "/"

    if not text.startswith("/"):
        text = "/" + text

    allowed = []
    for char in text:
        if char.isalnum() or char in {"/", "_", "-", "."}:
            allowed.append(char)

    text = "".join(allowed)

    if not text:
        return "/"

    if not text.startswith("/"):
        text = "/" + text

    if len(text) > 1:
        text = text.rstrip("/")

    return text or "/"


def normalize_port(value: Any, default: int = 8090) -> int:
    try:
        port = int(value)
    except Exception:
        return default

    if 1 <= port <= 65535:
        return port

    return default


def is_static_route(route: dict[str, Any]) -> bool:
    route_type = str(route.get("type") or "").strip().lower()

    return route_type in {
        "static",
        "web",
        "webserver",
        "web_server",
        "file_server",
        "files",
    }


def route_display_type(route: dict[str, Any]) -> str:
    if is_static_route(route):
        return "static"

    return "proxy"


def route_display_balancing(route: dict[str, Any]) -> str:
    if is_static_route(route):
        return "single/static"

    balancing = route.get("balancing")

    if balancing:
        return str(balancing)

    upstreams = route.get("upstreams")

    if isinstance(upstreams, list) and len(upstreams) > 1:
        return "round_robin"

    return "single/static"


def route_display_upstreams(route: dict[str, Any]) -> str:
    if is_static_route(route):
        root = route.get("root") or "public"
        index = route.get("index") or "index.html"
        return f"{root}/{index}"

    upstreams = route.get("upstreams")

    if isinstance(upstreams, list) and upstreams:
        parts: list[str] = []

        for item in upstreams:
            if isinstance(item, dict):
                address = item.get("address") or item.get("upstream") or item.get("backend") or ""
                weight = item.get("weight")

                if weight is not None:
                    parts.append(f"{address} weight {weight}")
                else:
                    parts.append(str(address))
            else:
                parts.append(str(item))

        return ", ".join(part for part in parts if part)

    upstream = route.get("upstream") or route.get("backend") or "-"

    return str(upstream)


def get_static_root(config: dict[str, Any]) -> str:
    routes = config.get("routes") or []

    if isinstance(routes, list):
        for route in routes:
            if not isinstance(route, dict):
                continue

            if normalize_path(route.get("path")) == "/" and is_static_route(route):
                return str(route.get("root") or "public").strip() or "public"

    return "public"


def get_static_index(config: dict[str, Any]) -> str:
    routes = config.get("routes") or []

    if isinstance(routes, list):
        for route in routes:
            if not isinstance(route, dict):
                continue

            if normalize_path(route.get("path")) == "/" and is_static_route(route):
                return str(route.get("index") or "index.html").strip() or "index.html"

    return "index.html"


def write_json_config(config: dict[str, Any], project_dir: str | Path) -> Path:
    config = ensure_route_directory_homepage(config)
    project_path = Path(project_dir)
    project_path.mkdir(parents=True, exist_ok=True)

    config_path = project_path / "config.json"
    tmp_path = config_path.with_suffix(".json.tmp")

    tmp_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    tmp_path.replace(config_path)

    return config_path



def public_safe_route_href(route_path: str) -> str:
    """
    Route-directory links must be relative so they work on:
    - local runtime: http://127.0.0.1:8090/api/
    - public IP:     http://132.145.51.57/api/
    - future domain: https://yourdomain.com/api/

    Never hardcode internal 127.0.0.1 URLs into clickable route links.
    """
    route_path = str(route_path or "/").strip()

    if not route_path.startswith("/"):
        route_path = "/" + route_path

    if route_path != "/" and not route_path.endswith("/"):
        route_path += "/"

    return route_path


def write_route_directory_homepage(config: dict[str, Any], project_dir: str | Path) -> Path:
    config = ensure_route_directory_homepage(config)
    """
    Always generate the route-directory homepage from config.

    This prevents "/" from falling back to the older simple page:
    "AI Pingora Webserver is running".

    project_writer.py owns the homepage.
    update_graph.py only changes config and redeploys.
    """

    project_path = Path(project_dir)
    static_root = get_static_root(config)
    static_index = get_static_index(config)

    public_dir = project_path / static_root
    index_path = public_dir / static_index

    public_dir.mkdir(parents=True, exist_ok=True)

    port = normalize_port(
        config.get("port")
        or config.get("listen_port")
        or config.get("proxy_port")
        or 8090,
        default=8090,
    )

    base_url = str(
        config.get("live_url")
        or config.get("public_url")
        or config.get("base_url")
        or f"http://127.0.0.1:{port}/"
    ).rstrip("/") + "/"

    routes = config.get("routes") or []

    if not isinstance(routes, list):
        routes = []

    normalized_routes: list[dict[str, Any]] = []

    for route in routes:
        if not isinstance(route, dict):
            continue

        fixed = dict(route)
        fixed["path"] = normalize_path(fixed.get("path") or "/")
        normalized_routes.append(fixed)

    rows: list[str] = []

    for route in normalized_routes:
        path = normalize_path(route.get("path") or "/")
        route_type = route_display_type(route)
        balancing = route_display_balancing(route)
        upstream_text = route_display_upstreams(route)

        url_path = "/" if path == "/" else path.rstrip("/") + "/"
        url = public_safe_route_href(path)

        rows.append(
            f"""
            <tr>
              <td><a href="{html.escape(url)}">{html.escape(path)}</a></td>
              <td>{html.escape(route_type)}</td>
              <td>{html.escape(balancing)}</td>
              <td><code>{html.escape(upstream_text)}</code></td>
            </tr>
            """
        )

    index_path.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>AI Pingora Gateway Dashboard</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root {{
      color-scheme: light;
      --bg: #f9fafb;
      --card: #ffffff;
      --text: #111827;
      --muted: #4b5563;
      --border: #e5e7eb;
      --link: #2563eb;
      --code-bg: #f3f4f6;
      --badge-bg: #dcfce7;
      --badge-text: #166534;
    }}

    * {{
      box-sizing: border-box;
    }}

    body {{
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      margin: 0;
      padding: 40px;
      background: var(--bg);
      color: var(--text);
    }}

    main {{
      max-width: 1180px;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 32px;
      box-shadow: 0 10px 25px rgba(0,0,0,0.06);
    }}

    h1 {{
      margin: 14px 0 12px;
      font-size: 34px;
      line-height: 1.15;
    }}

    p {{
      color: var(--muted);
      font-size: 16px;
    }}

    code {{
      background: var(--code-bg);
      padding: 2px 6px;
      border-radius: 6px;
      color: #111827;
    }}

    table {{
      width: 100%;
      border-collapse: collapse;
      margin-top: 24px;
      font-size: 15px;
    }}

    th, td {{
      text-align: left;
      border-bottom: 1px solid var(--border);
      padding: 12px 14px;
      vertical-align: top;
    }}

    th {{
      background: var(--code-bg);
      color: #374151;
      font-weight: 700;
    }}

    a {{
      color: var(--link);
      font-weight: 700;
      text-decoration: none;
    }}

    a:hover {{
      text-decoration: underline;
    }}

    .badge {{
      display: inline-block;
      background: var(--badge-bg);
      color: var(--badge-text);
      padding: 5px 11px;
      border-radius: 999px;
      font-weight: 700;
      font-size: 14px;
    }}

    .meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin-top: 16px;
    }}

    .meta-item {{
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px 12px;
      background: #ffffff;
    }}


    .dashboard-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
      gap: 16px;
      margin-top: 24px;
    }}

    .dash-card {{
      border: 1px solid var(--border);
      border-radius: 14px;
      background: #ffffff;
      padding: 18px;
      box-shadow: 0 6px 16px rgba(0,0,0,0.04);
    }}

    .dash-card h2 {{
      margin: 0 0 10px;
      font-size: 18px;
    }}

    .dash-card p {{
      margin: 8px 0;
      font-size: 14px;
    }}

    .ok-dot {{
      display: inline-block;
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: #22c55e;
      margin-right: 8px;
    }}

    .muted {{
      color: var(--muted);
    }}

    .command-center {{
      margin-top: 28px;
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 22px;
      background: #ffffff;
    }}

    .command-center h2 {{
      margin: 0 0 8px;
      font-size: 22px;
    }}

    .command-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
      gap: 12px;
      margin-top: 18px;
    }}

    .cmd-btn {{
      border: 1px solid var(--border);
      background: rgba(255, 255, 255, 0.72);
      color: var(--text);
      border-radius: 12px;
      padding: 12px 14px;
      font-weight: 700;
      cursor: pointer;
      text-align: left;
      transition: transform 0.16s ease, box-shadow 0.16s ease, border-color 0.16s ease, background 0.16s ease, color 0.16s ease;
    }}

    .cmd-btn:hover {{
      background: #ffffff;
      border-color: #22c55e;
      box-shadow: 0 0 0 3px rgba(34, 197, 94, 0.18), 0 12px 26px rgba(34, 197, 94, 0.22);
      transform: translateY(-2px);
    }}

    .cmd-btn:active,
    .cmd-btn.active {{
      background: #22c55e;
      border-color: #16a34a;
      color: #ffffff;
      box-shadow:
        0 0 0 4px rgba(34, 197, 94, 0.28),
        0 0 22px rgba(34, 197, 94, 0.42),
        0 18px 34px rgba(34, 197, 94, 0.32);
      transform: translateY(-4px) scale(1.025);
    }}

    .cmd-btn.active:hover {{
      background: #16a34a;
      border-color: #15803d;
      color: #ffffff;
      box-shadow:
        0 0 0 5px rgba(34, 197, 94, 0.30),
        0 0 28px rgba(34, 197, 94, 0.48),
        0 22px 40px rgba(34, 197, 94, 0.36);
      transform: translateY(-5px) scale(1.035);
    }}

    .cmd-btn.warning {{
      border-color: #f59e0b;
      background: #fffbeb;
    }}

    .cmd-btn.danger {{
      border-color: #ef4444;
      background: #fef2f2;
    }}

    .cmd-btn.live {{
      border-color: #22c55e;
      background: #f0fdf4;
    }}

    .command-output {{
      margin-top: 18px;
      border: 1px solid var(--border);
      border-radius: 14px;
      background: #111827;
      color: #e5e7eb;
      overflow: hidden;
    }}

    .output-title {{
      padding: 10px 14px;
      background: #1f2937;
      font-weight: 700;
    }}

    .command-output pre {{
      margin: 0;
      padding: 16px;
      overflow-x: auto;
      white-space: pre-wrap;
      font-size: 13px;
      line-height: 1.5;
    }}


    .control-api-config {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 12px;
      margin-top: 16px;
    }}

    .control-api-config label {{
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-weight: 700;
      font-size: 14px;
    }}

    .control-api-config input {{
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px 12px;
      font-size: 14px;
    }}

  </style>
</head>
<body>
  <main>
    <span class="badge">✅ Gateway live</span>
    <h1>AI Pingora Gateway Dashboard</h1>

    <p>This dashboard is served directly by the generated Pingora static webserver.</p>

    <div class="meta">
      <div class="meta-item">Served URL: <code id="servedUrl">{html.escape(base_url)}</code></div>
      <div class="meta-item">Served runtime: <code id="servedRuntime">detecting</code></div>
      <div class="meta-item">Config target: <code>{html.escape(str(config.get("runtime") or config.get("runtime_mode") or "local"))}</code></div>
      <div class="meta-item">Config strategy: <code>{html.escape(str(config.get("strategy") or config.get("deployment_strategy") or "direct"))}</code></div>
      <div class="meta-item">Total routes: <strong>{len(normalized_routes)}</strong></div>
    </div>


    <section class="dashboard-grid">
      <div class="dash-card">
        <h2>Runtime Health</h2>
        <p><span class="ok-dot"></span> Gateway responding</p>
        <p><a href="/__gateway_runtime_state">Open runtime state JSON</a></p>
      </div>

      <div class="dash-card" id="cacheCounterCard">
        <h2>Local In-Memory Response Cache</h2>
        <p>Hits: <strong id="cacheHitCount">0</strong></p>
        <p>Misses: <strong id="cacheMissCount">0</strong></p>
        <p>Entries: <strong id="cacheEntryCount">0</strong></p>
        <p>Bypasses: <strong id="cacheBypassCount">0</strong></p>
        <p>Eligible GET/HEAD: <strong id="cacheEligibleCount">0</strong></p>
        <p class="muted">Real local in-memory response cache. GET/HEAD only; Authorization and X-API-Key bypass cache.</p>
      </div>

      <div class="dash-card">
        <h2>Edge Mode</h2>
        <p>Pingora-only edge</p>
        <p class="muted">Production-style public HTTP port: <code>80</code></p>
      </div>

      <div class="dash-card">
        <h2>Decision Safety</h2>
        <p>Invalid mutations are blocked before deploy.</p>
        <p class="muted">Rejected changes create no backup, build, deploy, or rollback.</p>
      </div>

      <div class="dash-card">
        <h2>Operations</h2>
        <p><code>pingora-ai status</code></p>
        <p><code>pingora-ai stop</code></p>
        <p><code>pingora-ai test all</code></p>
      </div>

      <div class="dash-card">
        <h2>Backup Policy</h2>
        <p>Backups are created only for valid deployment attempts.</p>
        <p class="muted">Invalid mutations are blocked before backup.</p>
      </div>

      <div class="dash-card">
        <h2>Production Gateway Parity Target</h2>
        <p>Health checks, routing, blue/green, status, safety, and observability.</p>
        <p class="muted">Current mode: developer preview.</p>
      </div>
    </section>


    <section class="command-center">
      <h2>Gateway Operations</h2>
      <p class="muted">
        Safe dashboard mode: buttons show the command and result area. Runtime state can be fetched directly.
        Mutating actions still require terminal confirmation.
      </p>

      <div class="control-api-config">
        <label>
          Control API URL
          <input id="controlApiUrl" value="http://127.0.0.1:8787" />
        </label>
        <label>
          Control Token
          <input id="controlApiToken" value="dev-local-control-token" type="password" />
        </label>
      </div>

      <div class="command-grid">
        <button type="button" class="cmd-btn" data-command="pingora-ai status --docker" data-kind="api" data-endpoint="/__gateway_control/status">
          Docker Status
        </button>

        <button type="button" class="cmd-btn" data-command="pingora-ai status --local" data-kind="api" data-endpoint="/__gateway_control/status">
          Local Status
        </button>

        <button type="button" class="cmd-btn" data-command="pingora-ai status --linux --host <host> --user ubuntu --ssh-key <key>" data-kind="static">
          Linux Status
        </button>

        <button type="button" class="cmd-btn" data-command="pingora-ai parity" data-kind="api" data-endpoint="/__gateway_control/parity">
          Parity Report
        </button>

        <button type="button" class="cmd-btn" data-command="pingora-ai upstream list" data-kind="api" data-endpoint="/__gateway_control/upstreams">
          Upstream List
        </button>

        <button type="button" class="cmd-btn live" data-command="pingora-ai upstream live-state" data-kind="api" data-endpoint="/__gateway_control/upstreams">
          Live Upstream State
        </button>

        <button type="button" class="cmd-btn" data-command="pingora-ai doctor" data-kind="api" data-endpoint="/__gateway_control/doctor">
          Doctor Report
        </button>
        <button type="button" class="cmd-btn" data-command="pingora-ai performance report" data-kind="api" data-endpoint="/__gateway_control/performance?requests=20">
          Performance Report
        </button>
        <button type="button" class="cmd-btn" data-command="pingora-ai decision-integrity report" data-kind="api" data-endpoint="/__gateway_control/decision-integrity">
          Decision Integrity
        </button>
        <button type="button" class="cmd-btn" data-command="pingora-ai dashboard-check" data-kind="api" data-endpoint="/__gateway_control/dashboard-check">
          Dashboard Check
        </button>

        <button type="button" class="cmd-btn" data-command="pingora-ai health policy" data-kind="api" data-endpoint="/__gateway_control/health-policy">
          Health Policy
        </button>

        <button type="button" class="cmd-btn" data-command="pingora-ai backup list" data-kind="api" data-endpoint="/__gateway_control/backups">
          Backup List
        </button>

        <button type="button" class="cmd-btn" data-command="pingora-ai restart --docker" data-kind="static">
          Docker Restart
        </button>

        <button type="button" class="cmd-btn" data-command="pingora-ai reload --docker" data-kind="static">
          Docker Reload
        </button>

        <button type="button" class="cmd-btn warning" data-command="pingora-ai upstream drain /api 127.0.0.1:9101 --dry-run" data-kind="api" data-endpoint="/__gateway_control/upstream/drain/dry-run?route=/api&upstream=host.docker.internal:9101">
          Drain Upstream
        </button>

        <button type="button" class="cmd-btn warning" data-command="pingora-ai upstream disable /api 127.0.0.1:9101 --dry-run" data-kind="api" data-endpoint="/__gateway_control/upstream/disable/dry-run?route=/api&upstream=host.docker.internal:9101">
          Disable Upstream
        </button>

        <button type="button" class="cmd-btn warning" data-command="pingora-ai upstream enable /api 127.0.0.1:9101 --dry-run" data-kind="api" data-endpoint="/__gateway_control/upstream/enable/dry-run?route=/api&upstream=host.docker.internal:9101">
          Enable Upstream
        </button>


        <button type="button" class="cmd-btn danger" data-command="pingora-ai runtime-admin disable /api 127.0.0.1:9101 --confirm" data-kind="api" data-endpoint="/__gateway_control/runtime-admin/upstream/disable?route=/api&upstream=127.0.0.1:9101&confirm=true">
          Live Disable Upstream
        </button>

        <button type="button" class="cmd-btn danger" data-command="pingora-ai runtime-admin drain /api 127.0.0.1:9101 --confirm" data-kind="api" data-endpoint="/__gateway_control/runtime-admin/upstream/drain?route=/api&upstream=127.0.0.1:9101&confirm=true">
          Live Drain Upstream
        </button>

        <button type="button" class="cmd-btn warning" data-command="pingora-ai runtime-admin enable /api 127.0.0.1:9101 --confirm" data-kind="api" data-endpoint="/__gateway_control/runtime-admin/upstream/enable?route=/api&upstream=127.0.0.1:9101&confirm=true">
          Live Enable Upstream
        </button>

        <button type="button" class="cmd-btn warning" data-command="pingora-ai cache clear --dry-run" data-kind="api" data-endpoint="/__gateway_control/cache/clear/dry-run">
          Clear Cache Dry Run
        </button>

        <button type="button" class="cmd-btn danger" data-command="pingora-ai cache clear --confirm" data-kind="api" data-endpoint="/__gateway_control/cache/clear?confirm=true">
          Clear Cache Live
        </button>

        <button type="button" class="cmd-btn warning" data-command="pingora-ai backup restore generated-pingora-proxy-stable-final-restore-dashboard-20260607T201416Z --dry-run" data-kind="api" data-endpoint="/__gateway_control/backup/restore/dry-run?backup=generated-pingora-proxy-stable-final-restore-dashboard-20260607T201416Z">
          Restore Backup Dry Run
        </button>

        <button type="button" class="cmd-btn danger" data-command="pingora-ai backup restore generated-pingora-proxy-stable-final-restore-dashboard-20260607T201416Z --confirm" data-kind="mutation" data-endpoint="/__gateway_control/backup/restore/confirm?backup=generated-pingora-proxy-stable-final-restore-dashboard-20260607T201416Z&confirm=RESTORE%20BACKUP">
          Confirmed Restore Backup
        </button>

        <button type="button" class="cmd-btn danger" data-command="pingora-ai backup restore <backup_name> --confirm" data-kind="mutation">
          Restore Backup
        </button>

        <button type="button" class="cmd-btn live" id="runtimeStateButton">
          Fetch Runtime State
        </button>
      </div>

      <div class="command-output">
        <div class="output-title">Command Result</div>
        <pre id="commandResult">Click a button to view the command and result here.</pre>
      </div>
    </section>

    <table>
      <thead>
        <tr>
          <th>Route</th>
          <th>Type</th>
          <th>Balancing</th>
          <th>Upstreams / Static file</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows)}
      </tbody>
    </table>

    <p class="muted">
      This directory is regenerated from <code>config.json</code> on every project write/update.
    </p>
  </main>

  <script>
    (function () {{
      const output = document.getElementById("commandResult");

      function addExplicitDashboardContext(endpoint) {{
        const params = new URLSearchParams(window.location.search);
        const runtimeContext = params.get("runtime_context");
        const addressMode = params.get("address_mode");

        if (!runtimeContext && !addressMode) {{
          return endpoint;
        }}

        const extra = new URLSearchParams();

        if (runtimeContext) {{
          extra.set("runtime_context", runtimeContext);
        }}

        if (addressMode) {{
          extra.set("address_mode", addressMode);
        }}

        const joiner = endpoint.includes("?") ? "&" : "?";
        return endpoint + joiner + extra.toString();
      }}

      async function fetchControlApi(endpoint) {{
        endpoint = addExplicitDashboardContext(endpoint);

        const params = new URLSearchParams(window.location.search);
        if (
          endpoint.startsWith("/__gateway_control/upstreams") &&
          params.get("runtime_context") === "local" &&
          params.get("address_mode") === "local"
        ) {{
          const response = await fetch("/__gateway_runtime_state", {{ cache: "no-store" }});
          const text = await response.text();
          try {{
            return JSON.stringify(JSON.parse(text), null, 2);
          }} catch (_err) {{
            return text;
          }}
        }}

        const apiUrlInput = document.getElementById("controlApiUrl");
        const tokenInput = document.getElementById("controlApiToken");
        let baseUrl = apiUrlInput && apiUrlInput.value ? apiUrlInput.value : "http://127.0.0.1:8787";
        if (baseUrl.endsWith("/")) {{
          baseUrl = baseUrl.slice(0, -1);
        }}
        const token = tokenInput && tokenInput.value ? tokenInput.value : "";

        const response = await fetch(baseUrl + endpoint, {{
          method: "GET",
          cache: "no-store",
          headers: {{
            "X-Pingora-Control-Token": token
          }}
        }});

        const text = await response.text();
        if (!response.ok) {{
          throw new Error("HTTP " + response.status + ": " + text);
        }}

        try {{
          return JSON.stringify(JSON.parse(text), null, 2);
        }} catch (_err) {{
          return text;
        }}
      }}

      async function showCommand(button) {{
        const command = button.getAttribute("data-command");
        const kind = button.getAttribute("data-kind");
        const endpoint = button.getAttribute("data-endpoint");

        if (kind === "api" && endpoint) {{
          output.textContent = "Command:\\n" + command + "\\n\\nFetching Control API: " + endpoint + " ...";
          try {{
            output.textContent = await fetchControlApi(endpoint);
          }} catch (err) {{
            output.textContent =
              "Command:\\n" + command +
              "\\n\\nControl API error:\\n" + err +
              "\\n\\nMake sure this is running:\\npingora-ai control-api --token dev-local-control-token";
          }}
          return;
        }}

        const safety = kind === "mutation"
          ? "\\n\\nSafety: this is a mutating operation. Run it in the terminal with confirmation. The dashboard will not execute it directly."
          : "\\n\\nSafe: this command is read-only or lifecycle-only. Run it in the terminal.";

        output.textContent =
          "Command:\\n" +
          command +
          safety +
          "\\n\\nResult:\\nWaiting for terminal execution or a future authenticated control API.";
      }}

      document.querySelectorAll(".cmd-btn[data-command]").forEach((button) => {{
        button.addEventListener("click", () => {{
          document.querySelectorAll(".cmd-btn.active").forEach((item) => {{
            item.classList.remove("active");
          }});
          button.classList.add("active");

          if (button.id === "runtimeStateButton") {{
            return;
          }}
          showCommand(button);
        }});
      }});

      const runtimeButton = document.getElementById("runtimeStateButton");
      if (runtimeButton) {{
        runtimeButton.addEventListener("click", async (event) => {{
          event.preventDefault();
          event.stopPropagation();

          document.querySelectorAll(".cmd-btn.active").forEach((item) => {{
            item.classList.remove("active");
          }});
          runtimeButton.classList.add("active");

          output.textContent = "Fetching /__gateway_runtime_state ...";
          try {{
            const response = await fetch("/__gateway_runtime_state", {{ cache: "no-store" }});
            const text = await response.text();
            try {{
              output.textContent = JSON.stringify(JSON.parse(text), null, 2);
            }} catch (_err) {{
              output.textContent = text;
            }}
          }} catch (err) {{
            output.textContent = "Failed to fetch runtime state: " + err;
          }}
        }});
      }}

      async function updateCacheCountersFromRuntimeState() {{
        try {{
          const response = await fetch("/__gateway_runtime_state", {{ cache: "no-store" }});
          const data = await response.json();
          const cache = data && data.cache ? data.cache : {{}};

          const eligibleEl = document.getElementById("cacheEligibleCount");
          const bypassEl = document.getElementById("cacheBypassCount");
          const hitEl = document.getElementById("cacheHitCount");
          const missEl = document.getElementById("cacheMissCount");
          const entryEl = document.getElementById("cacheEntryCount");

          if (eligibleEl) {{
            eligibleEl.textContent = String(Number(cache.eligible || 0));
          }}

          if (bypassEl) {{
            bypassEl.textContent = String(Number(cache.bypasses || 0));
          }}

          if (hitEl) {{
            hitEl.textContent = String(Number(cache.hits || 0));
          }}

          if (missEl) {{
            missEl.textContent = String(Number(cache.misses || 0));
          }}

          if (entryEl) {{
            entryEl.textContent = String(Number(cache.entries || 0));
          }}
        }} catch (err) {{
          console.warn("cache counter refresh failed", err);
        }}
      }}

      updateCacheCountersFromRuntimeState();
      setInterval(updateCacheCountersFromRuntimeState, 5000);
    }})();
  </script>


  <script>
    (function () {{
      if (window.__pingoraDashboardDelegatedClicks) {{
        return;
      }}
      window.__pingoraDashboardDelegatedClicks = true;

      async function pingoraCallControlApi(endpoint) {{
        const params = new URLSearchParams(window.location.search);
        const runtimeContext = params.get("runtime_context");
        const addressMode = params.get("address_mode");

        if (runtimeContext || addressMode) {{
          const extra = new URLSearchParams();

          if (runtimeContext) {{
            extra.set("runtime_context", runtimeContext);
          }}

          if (addressMode) {{
            extra.set("address_mode", addressMode);
          }}

          const joiner = endpoint.includes("?") ? "&" : "?";
          endpoint = endpoint + joiner + extra.toString();
        }}

        const apiUrlInput = document.getElementById("controlApiUrl");
        const tokenInput = document.getElementById("controlApiToken");

        let baseUrl = apiUrlInput && apiUrlInput.value ? apiUrlInput.value : "http://127.0.0.1:8787";
        if (baseUrl.endsWith("/")) {{
          baseUrl = baseUrl.slice(0, -1);
        }}

        const token = tokenInput && tokenInput.value ? tokenInput.value : "dev-local-control-token";

        const response = await fetch(baseUrl + endpoint, {{
          method: "GET",
          cache: "no-store",
          headers: {{
            "X-Pingora-Control-Token": token
          }}
        }});

        const responseText = await response.text();

        if (!response.ok) {{
          throw new Error("HTTP " + response.status + ": " + responseText);
        }}

        try {{
          return JSON.stringify(JSON.parse(responseText), null, 2);
        }} catch (_err) {{
          return responseText;
        }}
      }}

      document.addEventListener("click", async function (event) {{
        const button = event.target.closest(".cmd-btn");
        if (!button) {{
          return;
        }}

        if (button.id === "runtimeStateButton") {{
          return;
        }}

        const output = document.getElementById("commandResult");
        if (!output) {{
          alert("Command result box not found.");
          return;
        }}

        const kind = button.getAttribute("data-kind") || "";
        const endpoint = button.getAttribute("data-endpoint") || "";
        const command = button.getAttribute("data-command") || "";

        if (kind === "api" && endpoint) {{
          output.textContent = "Fetching " + endpoint + " ...";

          try {{
            output.textContent = await pingoraCallControlApi(endpoint);
          }} catch (err) {{
            output.textContent =
              "Control API error:\\n" + err +
              "\\n\\nCheck Control API is running:\\npingora-ai control-api --token dev-local-control-token --reload";
          }}

          return;
        }}

        output.textContent =
          "Command:\\n" + command +
          "\\n\\nSafety: dashboard does not execute this mutation yet.";
      }});
    }})();
  </script>


  <script>
    (function () {{
      const port = window.location.port || (window.location.protocol === "https:" ? "443" : "80");
      const servedUrl = document.getElementById("servedUrl");
      const servedRuntime = document.getElementById("servedRuntime");

      let label = "unknown runtime";

      if (port === "8080" || port === "8081") {{
        label = "local/manual runtime";
      }} else if (port === "80" || port === "443") {{
        label = "public edge runtime";
      }} else if (port === "8090" || port === "8443") {{
        label = "legacy/internal runtime";
      }} else {{
        label = "custom runtime";
      }}

      if (servedRuntime) {{
        servedRuntime.textContent = label + " on port " + port;
      }}

      if (servedUrl) {{
        servedUrl.textContent = window.location.origin + "/";
      }}
    }})();
  </script>

</body>
</html>
""",
        encoding="utf-8",
    )

    return index_path


def _should_skip_copy(name: str) -> bool:
    return name in COPY_EXCLUDE_NAMES or name in COPY_EXCLUDE_FILES


def _copy_file_or_dir(src: Path, dst: Path) -> None:
    if _should_skip_copy(src.name):
        return

    if src.is_dir():
        if dst.exists():
            shutil.rmtree(dst)

        shutil.copytree(
            src,
            dst,
            ignore=shutil.ignore_patterns(
                *COPY_EXCLUDE_NAMES,
                *COPY_EXCLUDE_FILES,
                "*.log",
                "*.pyc",
            ),
        )
        return

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _runtime_candidate_dirs(project_dir: Path) -> list[Path]:
    candidates: list[Path] = []

    if DEFAULT_RUNTIME_TEMPLATE_DIR.exists():
        candidates.append(DEFAULT_RUNTIME_TEMPLATE_DIR)

    # For local staged updates, reuse the known-good generated runtime source.
    # This preserves the working Pingora implementation while config/homepage update.
    if DEFAULT_PROJECT_DIR.exists():
        candidates.append(DEFAULT_PROJECT_DIR)

    # Avoid duplicates while preserving order.
    unique: list[Path] = []
    seen: set[Path] = set()

    for candidate in candidates:
        resolved = candidate.resolve()

        if resolved == project_dir.resolve():
            continue

        if resolved in seen:
            continue

        seen.add(resolved)
        unique.append(candidate)

    return unique


def _has_runtime_source(project_dir: Path) -> bool:
    return (project_dir / "Cargo.toml").exists() and (project_dir / "src" / "main.rs").exists()


def _copy_runtime_scaffold(project_dir: Path) -> bool:
    """
    Copy the working Rust/Pingora runtime scaffold into a new project/staging dir.

    Preferred source:
    1. templates/pingora_runtime, if present.
    2. current generated-pingora-proxy, if present.

    This is deliberate: the Rust runtime is already verified locally. The writer
    should update config and homepage without accidentally replacing the runtime
    with an older/simple static page implementation.
    """

    if _has_runtime_source(project_dir):
        return True

    for candidate in _runtime_candidate_dirs(project_dir):
        if not _has_runtime_source(candidate):
            continue

        project_dir.mkdir(parents=True, exist_ok=True)

        for item in candidate.iterdir():
            if _should_skip_copy(item.name):
                continue

            _copy_file_or_dir(item, project_dir / item.name)

        if _has_runtime_source(project_dir):
            return True

    return False


def _write_minimal_runtime_placeholder(project_dir: Path) -> None:
    """
    Fallback only for brand-new empty checkouts where no runtime scaffold exists.

    In the normal project flow, generated-pingora-proxy or templates/pingora_runtime
    should provide the real Pingora runtime source.
    """

    src_dir = project_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    (project_dir / "Cargo.toml").write_text(
        """[package]
name = "generated-pingora-proxy"
version = "0.1.0"
edition = "2021"

[dependencies]
""",
        encoding="utf-8",
    )

    (src_dir / "main.rs").write_text(
        """fn main() {
    eprintln!("No Pingora runtime scaffold was found.");
    eprintln!("Expected templates/pingora_runtime or an existing generated-pingora-proxy runtime.");
    std::process::exit(1);
}
""",
        encoding="utf-8",
    )


def _write_generated_readme(project_dir: Path) -> None:
    readme = project_dir / "README.generated.md"

    readme.write_text(
        """# Generated AI Pingora Gateway

This folder is generated by the AI Pingora Gateway project.

Important files:

- `config.json` - generated gateway configuration
- `public/index.html` - route-directory homepage generated from config
- `src/main.rs` - Rust/Pingora runtime source
- `Cargo.toml` - Rust project manifest

Do not edit generated files manually unless debugging.
""",
        encoding="utf-8",
    )


def _write_project_gitignore(project_dir: Path) -> None:
    path = project_dir / ".gitignore"

    path.write_text(
        """target/
.runtime/
tmp/
*.log
.env
""",
        encoding="utf-8",
    )


def _maybe_write_demo_backend_files(config: dict[str, Any], project_dir: Path) -> None:
    """
    Optional compatibility with existing demo backend writer.

    The actual runtime backend servers are started by scripts/demo_backends.py.
    This optional step only preserves any existing file-generation behaviour.
    """

    demo_config = config.get("demo_backend") or {}

    if isinstance(demo_config, dict):
        enabled = bool(demo_config.get("enabled", True))
    else:
        enabled = True

    if not enabled:
        return

    try:
        from core.demo_backend_writer import write_demo_backend_files
    except Exception:
        return

    try:
        write_demo_backend_files(config, project_dir=project_dir)
    except TypeError:
        try:
            write_demo_backend_files(config, project_dir)
        except TypeError:
            write_demo_backend_files(config)
    except Exception as exc:
        print(f"⚠️ Demo backend file generation skipped: {exc}")



# ---------------------------------------------------------------------
# Rust build_routes regeneration
# ---------------------------------------------------------------------


def _pw_rust_string(value):
    text = "" if value is None else str(value)
    text = text.replace("\\", "\\\\")
    text = text.replace('"', '\\"')
    text = text.replace("\n", "\\n")
    text = text.replace("\r", "\\r")
    return f'"{text}"'


def _pw_normalize_path(value):
    text = str(value or "/").strip()

    if not text.startswith("/"):
        text = "/" + text

    allowed = []

    for char in text:
        if char.isalnum() or char in {"/", "_", "-", "."}:
            allowed.append(char)

    text = "".join(allowed) or "/"

    if len(text) > 1:
        text = text.rstrip("/")

    return text or "/"


def _pw_is_static_route(route):
    route_type = str(route.get("type") or "").strip().lower()

    return route_type in {
        "static",
        "web",
        "webserver",
        "web_server",
        "file_server",
        "files",
    }


def _pw_route_type_for_rust(route):
    return "static" if _pw_is_static_route(route) else "proxy"


def _pw_balancing_for_rust(route):
    balancing = route.get("balancing")

    if balancing:
        return str(balancing)

    return "round_robin"


def _pw_upstreams_for_rust(route):
    if _pw_is_static_route(route):
        return []

    upstreams = route.get("upstreams")
    output = []

    if isinstance(upstreams, list):
        for item in upstreams:
            if isinstance(item, dict):
                address = (
                    item.get("address")
                    or item.get("upstream")
                    or item.get("backend")
                    or ""
                )

                if not address:
                    continue

                try:
                    weight = int(item.get("weight", 1))
                except Exception:
                    weight = 1

                output.append(
                    {
                        "address": str(address),
                        "weight": max(weight, 1),
                    }
                )
            else:
                address = str(item or "").strip()

                if address:
                    output.append(
                        {
                            "address": address,
                            "weight": 1,
                        }
                    )

    if output:
        return output

    address = route.get("upstream") or route.get("backend")

    if address:
        return [
            {
                "address": str(address),
                "weight": 1,
            }
        ]

    return []



def _pw_rust_string_list(values: Any, fallback: list[str]) -> str:
    if not isinstance(values, list) or not values:
        values = fallback

    cleaned = []
    for value in values:
        item = str(value or "").strip()
        if item:
            cleaned.append(item)

    if not cleaned:
        cleaned = fallback

    return "vec![" + ", ".join(f'"{item}".to_string()' for item in cleaned) + "]"


def _pw_cache_to_rust(route: dict[str, Any]) -> str:
    cache = route.get("cache") if isinstance(route, dict) else None

    if not isinstance(cache, dict):
        return "Some(default_cache_config())"

    enabled = "true" if bool(cache.get("enabled", False)) else "false"

    try:
        ttl_seconds = int(cache.get("ttl_seconds", 30))
    except Exception:
        ttl_seconds = 30
    ttl_seconds = max(1, min(86400, ttl_seconds))

    try:
        stale_seconds = int(cache.get("stale_while_revalidate_seconds", 0))
    except Exception:
        stale_seconds = 0
    stale_seconds = max(0, min(3600, stale_seconds))

    cache_key = str(cache.get("cache_key") or "method:path:query").strip()
    if cache_key not in {"method:path:query", "method:path"}:
        cache_key = "method:path:query"

    methods = _pw_rust_string_list(cache.get("methods"), ["GET"])
    bypass_headers = _pw_rust_string_list(
        cache.get("bypass_headers"),
        ["authorization", "x-api-key"],
    )

    return (
        "Some(CacheConfig { "
        f"enabled: {enabled}, "
        f"ttl_seconds: {ttl_seconds}, "
        f"methods: {methods}, "
        f'cache_key: "{cache_key}".to_string(), '
        f"bypass_headers: {bypass_headers}, "
        f"stale_while_revalidate_seconds: {stale_seconds}, "
        "})"
    )


def _pw_route_to_rust(route):
    path = _pw_normalize_path(route.get("path") or "/")
    route_type = _pw_route_type_for_rust(route)
    root = str(route.get("root") or "public")
    index = str(route.get("index") or "index.html")
    balancing = _pw_balancing_for_rust(route)
    upstreams = _pw_upstreams_for_rust(route)

    lines = [
        "    RouteConfig {",
        f"        path: {_pw_rust_string(path)}.to_string(),",
        f"        route_type: {_pw_rust_string(route_type)}.to_string(),",
        f"        root: {_pw_rust_string(root)}.to_string(),",
        f"        index: {_pw_rust_string(index)}.to_string(),",
        f"        balancing: {_pw_rust_string(balancing)}.to_string(),",
        f"        cache: {_pw_cache_to_rust(route)},",
        "        upstreams: vec![",
    ]

    for upstream in upstreams:
        address = str(upstream.get("address") or "")
        weight = int(upstream.get("weight") or 1)

        state = str(upstream.get("state") or "enabled").lower()
        draining = bool(upstream.get("draining", state in {"draining", "drain"}))
        role = str(upstream.get("role") or ("backup" if upstream.get("backup") else "primary")).lower()
        backup = bool(upstream.get("backup", role in {"backup", "standby", "secondary"}))

        enabled = upstream.get("enabled")
        if enabled is None:
            enabled = state not in {"disabled", "draining", "drain"}
        enabled = bool(enabled)

        lines.extend(
            [
                "        UpstreamConfig {",
                f"            address: {_pw_rust_string(address)}.to_string(),",
                f"            weight: {weight},",
                f"            state: {_pw_rust_string(state)}.to_string(),",
                f"            enabled: {str(enabled).lower()},",
                f"            draining: {str(draining).lower()},",
                f"            role: {_pw_rust_string(role)}.to_string(),",
                f"            backup: {str(backup).lower()},",
                "        },",
            ]
        )

    lines.extend(
        [
            "        ],",
            "    },",
        ]
    )

    return "\n".join(lines)


def _pw_build_routes_function_from_config(config):
    routes = config.get("routes") or []

    if not isinstance(routes, list):
        routes = []

    route_blocks = []

    for route in routes:
        if isinstance(route, dict):
            route_blocks.append(_pw_route_to_rust(route))

    joined = "\n".join(route_blocks)

    return (
        "fn build_routes() -> Vec<RouteConfig> {\n"
        "    vec![\n"
        f"{joined}\n"
        "    ]\n"
        "}\n"
    )



def rewrite_rust_listen_port(config: dict, project_path: Path) -> None:
    """
    Rewrite generated Rust listen port from config.

    This supports native public webserver mode:
      pingora-ai apply --local --public-port 80 --systemd ...
    """
    try:
        port = int(
            config.get("port")
            or config.get("public_port")
            or config.get("listen_port")
            or 8090
        )
    except Exception:
        port = 8090

    if port < 1 or port > 65535:
        port = 8090

    main_rs = project_path / "src" / "main.rs"
    text = main_rs.read_text(encoding="utf-8")

    if "const GATEWAY_LISTEN_PORT:" not in text:
        text = text.replace(
            'const HEALTH_CHECK_PATH: &str = "/health";',
            f'const GATEWAY_LISTEN_PORT: u16 = {port};\nconst HEALTH_CHECK_PATH: &str = "/health";',
            1,
        )
    else:
        text = re.sub(
            r"const GATEWAY_LISTEN_PORT:\s*u16\s*=\s*\d+;",
            f"const GATEWAY_LISTEN_PORT: u16 = {port};",
            text,
            count=1,
        )

    # Safety: ensure the runtime uses the generated constant, not hardcoded 8090.
    text = text.replace(
        "let listen_port = config.port.unwrap_or(8090);",
        "let listen_port = GATEWAY_LISTEN_PORT;",
    )
    text = text.replace(
        'service.add_tcp("0.0.0.0:8090");',
        'service.add_tcp(&listen_addr);',
    )

    main_rs.write_text(text, encoding="utf-8")


def rewrite_rust_build_routes(config, project_dir):
    config = ensure_route_directory_homepage(config)
    project_path = Path(project_dir)
    main_rs = project_path / "src" / "main.rs"

    if not main_rs.exists():
        raise FileNotFoundError(f"Cannot rewrite routes; missing {main_rs}")

    text = main_rs.read_text(encoding="utf-8")
    start = text.find("fn build_routes() -> Vec<RouteConfig> {")

    if start == -1:
        raise RuntimeError("Could not find build_routes() in src/main.rs")

    end = text.find("\nfn build_security", start)

    if end == -1:
        match = re.search(r"\nfn\s+[A-Za-z0-9_]+\s*\(", text[start + 1:])

        if not match:
            raise RuntimeError("Could not find end of build_routes() in src/main.rs")

        end = start + 1 + match.start()

    new_fn = _pw_build_routes_function_from_config(config)
    updated = text[:start] + new_fn + text[end:]
    main_rs.write_text(updated, encoding="utf-8")

    return main_rs


# ---------------------------------------------------------------------
# End Rust build_routes regeneration


def write_project(
    config: dict[str, Any],
    project_dir: str | Path = DEFAULT_PROJECT_DIR,
    **_: Any,
) -> Path:
    config = ensure_route_directory_homepage(config)
    """
    Write/update a generated gateway project.

    This function intentionally does three key things:

    1. Preserves/copies the verified Rust/Pingora runtime scaffold.
    2. Writes the latest config.json.
    3. Always regenerates public/index.html as a route-directory homepage.

    This prevents update/delete/edit deployments from replacing the homepage with
    the older generic static page.
    """

    if not isinstance(config, dict):
        raise TypeError("write_project expected config to be a dict")

    project_path = Path(project_dir)
    project_path.mkdir(parents=True, exist_ok=True)

    copied_runtime = _copy_runtime_scaffold(project_path)

    if not copied_runtime:
        _write_minimal_runtime_placeholder(project_path)

    config_path = write_json_config(config, project_path)
    rewrite_rust_build_routes(config, project_path)
    rewrite_rust_listen_port(config, project_path)
    homepage_path = write_route_directory_homepage(config, project_path)

    _write_generated_readme(project_path)
    _write_project_gitignore(project_path)
    _maybe_write_demo_backend_files(config, project_path)

    print("✅ Project generated successfully")
    print(f"📁 Folder: {project_path.resolve()}")
    print(f"🧭 Config: {config_path.resolve()}")
    print(f"🏠 Route directory: {homepage_path.resolve()}")

    return project_path


# Compatibility aliases used by older orchestration code.


def generate_project(config: dict[str, Any], project_dir: str | Path = DEFAULT_PROJECT_DIR, **kwargs: Any) -> Path:
    return write_project(config, project_dir=project_dir, **kwargs)


def write_pingora_project(config: dict[str, Any], project_dir: str | Path = DEFAULT_PROJECT_DIR, **kwargs: Any) -> Path:
    return write_project(config, project_dir=project_dir, **kwargs)


def generate_pingora_project(config: dict[str, Any], project_dir: str | Path = DEFAULT_PROJECT_DIR, **kwargs: Any) -> Path:
    return write_project(config, project_dir=project_dir, **kwargs)


def create_project(config: dict[str, Any], project_dir: str | Path = DEFAULT_PROJECT_DIR, **kwargs: Any) -> Path:
    return write_project(config, project_dir=project_dir, **kwargs)


def write(config: dict[str, Any], project_dir: str | Path = DEFAULT_PROJECT_DIR, **kwargs: Any) -> Path:
    return write_project(config, project_dir=project_dir, **kwargs)


# ---------------------------------------------------------------------
# Compatibility renderer for compose_writer.py
# ---------------------------------------------------------------------


def render_main_rs(config: dict[str, Any]) -> str:
    config = ensure_route_directory_homepage(config)
    """
    Compatibility function used by core.compose_writer.

    Some older Docker/Compose generation code imports render_main_rs()
    directly from project_writer.py. The current writer updates src/main.rs
    by rewriting build_routes(), so this function now renders a complete
    main.rs string from the existing runtime scaffold and replaces build_routes()
    from config["routes"].
    """

    candidate_paths = [
        DEFAULT_RUNTIME_TEMPLATE_DIR / "src" / "main.rs",
        DEFAULT_PROJECT_DIR / "src" / "main.rs",
        PROJECT_ROOT / "generated-pingora-proxy" / "src" / "main.rs",
    ]

    source_path = None

    for candidate in candidate_paths:
        if candidate.exists():
            source_path = candidate
            break

    if source_path is None:
        raise FileNotFoundError(
            "Could not render main.rs because no runtime scaffold was found. "
            "Expected templates/pingora_runtime/src/main.rs or "
            "generated-pingora-proxy/src/main.rs."
        )

    text = source_path.read_text(encoding="utf-8")

    if "_pw_build_routes_function_from_config" in globals():
        new_build_routes = _pw_build_routes_function_from_config(config)
    elif "_build_routes_function_from_config" in globals():
        new_build_routes = _build_routes_function_from_config(config)
    else:
        raise RuntimeError(
            "Could not render main.rs because no build_routes renderer exists "
            "in core.project_writer.py."
        )

    start = text.find("fn build_routes() -> Vec<RouteConfig> {")

    if start == -1:
        raise RuntimeError("Could not find build_routes() in runtime scaffold main.rs")

    end = text.find("\nfn build_security", start)

    if end == -1:
        match = re.search(r"\nfn\s+[A-Za-z0-9_]+\s*\(", text[start + 1:])

        if not match:
            raise RuntimeError("Could not find end of build_routes() in runtime scaffold main.rs")

        end = start + 1 + match.start()

    return text[:start] + new_build_routes + text[end:]


# ---------------------------------------------------------------------
# End compatibility renderer


# ---------------------------------------------------------------------
# Compatibility helpers used by core.runner
# ---------------------------------------------------------------------


def collect_expected_upstream_addresses(config: dict[str, Any]) -> list[str]:
    """
    Return all upstream backend addresses expected by the generated config.

    Older runner/graph code imports this from project_writer.py during cargo
    checks and runtime validation.
    """

    addresses: list[str] = []
    routes = config.get("routes") or []

    if not isinstance(routes, list):
        return addresses

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
                    address = (
                        item.get("address")
                        or item.get("upstream")
                        or item.get("backend")
                    )
                else:
                    address = item

                if address:
                    text = str(address).strip().replace("http://", "").replace("https://", "").rstrip("/")

                    if "/" in text:
                        text = text.split("/", 1)[0]

                    if text and text not in addresses:
                        addresses.append(text)

        else:
            address = route.get("upstream") or route.get("backend")

            if address:
                text = str(address).strip().replace("http://", "").replace("https://", "").rstrip("/")

                if "/" in text:
                    text = text.split("/", 1)[0]

                if text and text not in addresses:
                    addresses.append(text)

    return addresses


# ---------------------------------------------------------------------
# End compatibility helpers


# ---------------------------------------------------------------------
# Post-generation upstream state patch
# ---------------------------------------------------------------------
# Safe purpose:
# - keep existing project writer intact
# - after generated-pingora-proxy/src/main.rs is written, ensure runtime supports:
#   disabled/draining upstreams and backup upstream fallback
#
# This is intentionally post-generation because this file uses an existing Rust
# runtime scaffold and only regenerates build_routes().
# ---------------------------------------------------------------------

def _pw_patch_runtime_upstream_state(main_rs_path):
    import re
    from pathlib import Path

    main_rs_path = Path(main_rs_path)

    if not main_rs_path.exists():
        return False

    text = main_rs_path.read_text(encoding="utf-8")
    original = text

    # 1) UpstreamConfig struct metadata.
    old_struct = """#[derive(Clone, Debug)]
struct UpstreamConfig {
    address: String,
    weight: usize,
}
"""

    new_struct = """#[derive(Clone, Debug)]
struct UpstreamConfig {
    address: String,
    weight: usize,
    state: String,
    enabled: bool,
    draining: bool,
    role: String,
    backup: bool,
}
"""

    if old_struct in text:
        text = text.replace(old_struct, new_struct, 1)

    # 2) UpstreamConfig route literals generated from config.
    # Handles generated blocks like:
    # UpstreamConfig { address: "...".to_string(), weight: 1, }
    # and multi-line forms.
    pattern = re.compile(
        r'UpstreamConfig\s*\{\s*address:\s*(?P<address>[^,]+),\s*weight:\s*(?P<weight>[^}]+?)\s*\}',
        flags=re.DOTALL,
    )

    def _upstream_literal_repl(match):
        literal = match.group(0)

        if "state:" in literal and "enabled:" in literal and "role:" in literal:
            return literal

        weight = match.group("weight").strip().rstrip(",")

        return (
            "UpstreamConfig { "
            f"address: {match.group('address').strip()}, "
            f"weight: {weight}, "
            'state: "enabled".to_string(), '
            "enabled: true, "
            "draining: false, "
            'role: "primary".to_string(), '
            "backup: false "
            "}"
        )

    text = pattern.sub(_upstream_literal_repl, text)

    # Defensive cleanup from earlier manual patch experiments.
    text = re.sub(r"(weight:\s*[^,\n}]+),,", r"\1,", text)

    # 3) Runtime helper functions.
    helper = """
fn pingora_ai_upstream_state_enabled(state: &str, enabled: bool, draining: bool) -> bool {
    if !enabled {
        return false;
    }

    if draining {
        return false;
    }

    !matches!(state.to_ascii_lowercase().as_str(), "disabled" | "draining" | "drain")
}

fn pingora_ai_upstream_role_is_backup(role: &str, backup: bool) -> bool {
    if backup {
        return true;
    }

    matches!(role.to_ascii_lowercase().as_str(), "backup" | "standby" | "secondary")
}

"""

    if "fn pingora_ai_upstream_state_enabled" not in text:
        marker = "impl GeneratedProxy {"
        if marker in text:
            text = text.replace(marker, helper + marker, 1)

    # 4) Selector logic: primary first, backup fallback, disabled/draining skipped.
    old_eligible = """    fn eligible_upstream_indices(&self, route_index: usize) -> Vec<usize> {
        let route = match self.routes.get(route_index) {
            Some(value) => value,
            None => return Vec::new(),
        };

        route
            .upstreams
            .iter()
            .enumerate()
            .filter_map(|(index, upstream)| {
                if self.upstream_is_healthy(route, upstream) {
                    Some(index)
                } else {
                    None
                }
            })
            .collect::<Vec<_>>()
    }
"""

    new_eligible = """    fn upstream_allows_new_traffic(&self, upstream: &UpstreamConfig) -> bool {
        pingora_ai_upstream_state_enabled(
            &upstream.state,
            upstream.enabled,
            upstream.draining,
        )
    }

    fn upstream_is_backup(&self, upstream: &UpstreamConfig) -> bool {
        pingora_ai_upstream_role_is_backup(
            &upstream.role,
            upstream.backup,
        )
    }

    fn eligible_upstream_indices_by_role(&self, route_index: usize, want_backup: bool) -> Vec<usize> {
        let route = match self.routes.get(route_index) {
            Some(value) => value,
            None => return Vec::new(),
        };

        route
            .upstreams
            .iter()
            .enumerate()
            .filter_map(|(index, upstream)| {
                if !self.upstream_allows_new_traffic(upstream) {
                    return None;
                }

                if self.upstream_is_backup(upstream) != want_backup {
                    return None;
                }

                if self.upstream_is_healthy(route, upstream) {
                    Some(index)
                } else {
                    None
                }
            })
            .collect::<Vec<_>>()
    }

    fn eligible_upstream_indices(&self, route_index: usize) -> Vec<usize> {
        let primary = self.eligible_upstream_indices_by_role(route_index, false);
        if !primary.is_empty() {
            return primary;
        }

        self.eligible_upstream_indices_by_role(route_index, true)
    }
"""

    if old_eligible in text:
        text = text.replace(old_eligible, new_eligible, 1)

    # 5) Runtime state JSON metadata, if exact old block exists.
    old_json = """                upstreams_json.push(format!(
                    "{{\\"address\\":\\"{}\\",\\"healthy\\":{},\\"consecutive_successes\\":{},\\"consecutive_failures\\":{},\\"last_status\\":{},\\"last_error\\":{},\\"last_checked_unix_ms\\":{}}}",
                    json_escape(&upstream.address),
                    item.healthy,
                    item.consecutive_successes,
                    item.consecutive_failures,
                    last_status_json,
                    last_error_json,
                    item.last_checked_unix_ms
                ));
"""

    new_json = """                upstreams_json.push(format!(
                    "{{\\"address\\":\\"{}\\",\\"weight\\":{},\\"state\\":\\"{}\\",\\"enabled\\":{},\\"draining\\":{},\\"role\\":\\"{}\\",\\"backup\\":{},\\"healthy\\":{},\\"consecutive_successes\\":{},\\"consecutive_failures\\":{},\\"last_status\\":{},\\"last_error\\":{},\\"last_checked_unix_ms\\":{}}}",
                    json_escape(&upstream.address),
                    upstream.weight,
                    json_escape(&upstream.state),
                    upstream.enabled,
                    upstream.draining,
                    json_escape(&upstream.role),
                    upstream.backup,
                    item.healthy,
                    item.consecutive_successes,
                    item.consecutive_failures,
                    last_status_json,
                    last_error_json,
                    item.last_checked_unix_ms
                ));
"""

    if old_json in text:
        text = text.replace(old_json, new_json, 1)

    if text != original:
        main_rs_path.write_text(text, encoding="utf-8")
        return True

    return False


# Wrap write_project_from_config safely.
# This avoids changing the main generation flow and keeps rollback simple.
if "write_project_from_config" in globals() and not globals().get("_PW_UPSTREAM_STATE_WRAPPED"):
    _PW_ORIGINAL_WRITE_PROJECT_FROM_CONFIG = write_project_from_config

    def write_project_from_config(*args, **kwargs):
        result = _PW_ORIGINAL_WRITE_PROJECT_FROM_CONFIG(*args, **kwargs)

        project_dir = kwargs.get("project_dir")

        if project_dir is None and len(args) >= 2:
            project_dir = args[1]

        if project_dir is None:
            project_dir = "generated-pingora-proxy"

        main_rs = Path(project_dir) / "src" / "main.rs"
        patched = _pw_patch_runtime_upstream_state(main_rs)

        if patched:
            print("✅ Runtime upstream state support patched into generated Rust.")

        return result

    _PW_UPSTREAM_STATE_WRAPPED = True


# ---------------------------------------------------------------------
# Real write_project wrapper for upstream state support
# ---------------------------------------------------------------------
# The project writer's public entrypoint is write_project(), not
# write_project_from_config(). Keep this wrapper small and reversible.
# ---------------------------------------------------------------------

if "write_project" in globals() and not globals().get("_PW_REAL_WRITE_PROJECT_UPSTREAM_STATE_WRAPPED"):
    _PW_ORIGINAL_WRITE_PROJECT = write_project

    def write_project(config, project_dir=DEFAULT_PROJECT_DIR, **kwargs):
        result = _PW_ORIGINAL_WRITE_PROJECT(config, project_dir=project_dir, **kwargs)

        try:
            main_rs = Path(project_dir) / "src" / "main.rs"
            patched = _pw_patch_runtime_upstream_state(main_rs)

            if patched:
                print("✅ Runtime upstream state support patched into generated Rust.")
        except Exception as exc:
            print(f"⚠️ Runtime upstream state post-generation patch skipped: {exc}")

        return result

    _PW_REAL_WRITE_PROJECT_UPSTREAM_STATE_WRAPPED = True


# ---------------------------------------------------------------------
# Runtime Admin Agent post-generation hook
# ---------------------------------------------------------------------

def _pw_run_post_generation_script(script_name, success_message, failure_label):
    """
    Run a verified generated-runtime patch script after write_project().

    Safe scope:
    - generated project only
    - no Docker apply
    - no Linux deploy
    - no restart
    """
    try:
        from pathlib import Path
        import subprocess
        import sys

        root = Path(__file__).resolve().parents[1]
        script = root / "scripts" / script_name

        if not script.exists():
            print(f"⚠️ {failure_label} skipped: script missing")
            return

        result = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(root),
            text=True,
            capture_output=True,
            timeout=30,
        )

        if result.returncode != 0:
            print(f"⚠️ {failure_label} failed")
            print(result.stdout[-2000:])
            print(result.stderr[-2000:])
            return

        print(success_message)
    except Exception as exc:
        print(f"⚠️ {failure_label} skipped: {exc}")


def _pw_patch_runtime_admin_agent_phase1(project_dir):
    _pw_run_post_generation_script(
        "patch_runtime_admin_agent_phase1.py",
        "✅ Runtime Admin Agent phase1 patched into generated Rust.",
        "Runtime Admin Agent patch",
    )


def _pw_patch_runtime_manual_cache_step6(project_dir):
    _pw_run_post_generation_script(
        "patch_runtime_manual_cache_step6.py",
        "✅ Manual cache Step 6 patched into generated Rust.",
        "Manual cache Step 6 patch",
    )


if "_pw_original_write_project_runtime_admin" not in globals():
    _pw_original_write_project_runtime_admin = write_project

    def write_project(config, project_dir=DEFAULT_PROJECT_DIR, **kwargs):
        result = _pw_original_write_project_runtime_admin(config, project_dir=project_dir, **kwargs)
        _pw_patch_runtime_admin_agent_phase1(project_dir)
        _pw_patch_runtime_manual_cache_step6(project_dir)
        return result

