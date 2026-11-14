# Linux Docker Compose deployment

Pingox provides a production deployment path through `pingox.py`. The old
`main.py`, `update.py`, self-healing and blue/green flows remain development tools;
they can launch demo backends and automatically repair generated code. Do not use
those flows to operate production traffic.

## Release status

This is a pre-1.0 release with tested deployment paths, not a blanket production certification.
Python regression tests and a real local Pingora binary are tested by the supplied
commands. Linux image build, public Caddy TLS issuance, your real backends, load capacity,
backup restoration and rollback must pass in your environment before cutover.
The GitHub workflow performs the Linux container smoke test on pushes/PRs.

## Architecture

Clients connect to Caddy on ports 80/443. Caddy manages HTTPS certificates and
forwards to a private Pingora service. Pingora connects to your existing backend
Docker network. No gateway port is published. Only Caddy and Pingora belong on the
edge network. AI credentials and tools stay on a separate build/control-plane
machine; the gateway runs without Python, Codex, an API key or an AI service.

## Prepare a reviewed release

Requirements on the build machine: Python 3.12+, Docker Engine with Compose v2.
Rust 1.95 is required only for local native tests; Docker builds supply it.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp deploy/gateway.example.json gateway-production.json
```

Edit `gateway-production.json`: use the actual backend service DNS names and ports
on your backend network. Upstreams can use HTTP on a trusted private network or verified HTTPS with
`tls` and `server_name` (see ENTERPRISE_DEPLOYMENT.md). IPv6 destinations,
WebSockets, encoded URL paths and static sites are
not supported by the strict production configuration. Unsupported inputs fail
instead of being silently rewritten. Query strings are forwarded unchanged.

For an optional AI proposal, configure `OPENAI_API_KEY` or the Codex backend as
explained in `WORKERS.md`, then run:

```sh
python pingox.py plan 'Listen on 8080 and route /api to api-backend:8000; allow GET HEAD POST' --output proposed.json
```

Review the JSON before using it. This command never deploys or overwrites a file.
All validation and generation also work with `AI_WORKER_BACKEND=disabled`.

```sh
python pingox.py validate --config gateway-production.json
python pingox.py generate --config gateway-production.json --output releases/release-001
cd releases/release-001
cp .env.example .env
```

Edit `.env`:

- `GATEWAY_IMAGE`: unique release tag, or registry image digest when deploying a prebuilt image.
- `CADDY_IMAGE`: an approved Caddy 2 image digest. Resolve and scan the image in your registry; do not leave this empty.
- `GATEWAY_DOMAIN`: your DNS hostname pointing to this Linux server.
- `ACME_EMAIL`: certificate contact address.
- `UPSTREAM_NETWORK`: existing Docker network containing your real backend services.
- `GATEWAY_NETWORK_CIDR`: unused subnet dedicated to the edge and gateway. Check it does not overlap host/VPN/backend networks.

The generated release includes a checked-in dependency lockfile; builds use
`cargo build --locked`. Store release directory, image digest, config and test
results together. Base image tags must be resolved to approved digests in your
release pipeline if bit-for-bit base-image reproducibility is required.

## Build and start

Open TCP 80/443 to clients, keep administrative access restricted, and ensure the
server can reach the certificate authority. Your backend must authenticate and
authorize application requests; optional gateway API keys provide admission only; JWT and application authorization remain your backend’s responsibility. See PINGOX_OPERATIONS.md.

```sh
docker compose -p ai-gateway config --quiet
docker compose -p ai-gateway build gateway
docker compose -p ai-gateway run --rm --no-deps edge caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose -p ai-gateway up -d --wait --wait-timeout 120
docker compose -p ai-gateway ps
curl --fail https://YOUR_DOMAIN/api/
```

For a prebuilt registry image, use `docker compose -p ai-gateway pull` then
`docker compose -p ai-gateway up -d --no-build --wait --wait-timeout 120`.

Verify a real API route, a blocked path, allowed/disallowed methods and a request
with its required application credentials. The internal `/_gateway/health`
endpoint is process health only; it does not certify backend availability.
Caddy blocks that endpoint from the public edge. Health checks bypass traffic
rate limits. Upstream failures return an error; there is no automatic replay of
POSTs and no active pool health checking or circuit breaker in this version.

## Operations and rollback

Use `docker compose -p ai-gateway logs --tail 100 gateway edge` for diagnostics.
Gateway access records include status, method, duration and failure flag; they
omit request bodies, query strings and credentials. Docker log rotation is set.
Alert externally on API availability, 5xx/429/503 rates, latency, container
restarts, memory use and certificate expiry. No Prometheus stack is bundled.

Create each new release in a separate directory and validate it on a staging
server first. Preserve the old release directory and old gateway/Caddy image
digests. Use the same Compose project name (`ai-gateway`) for replacement so
certificate volumes are retained. Replacing the single gateway can cause a short
interruption; this path does not claim zero-downtime blue/green updates.

Rollback from the previous release directory:

```sh
docker compose -p ai-gateway up -d --no-build --wait --wait-timeout 120
curl --fail https://YOUR_DOMAIN/api/
```

The previous `.env` must point to the retained old image. Never use `down -v` for
production rollback. Back up `caddy_data` (certificate material, sensitive),
`caddy_config`, reviewed configs and release metadata. Test restoration in a
separate environment.

## Limits to understand

- `max_connections` currently limits concurrent in-flight requests, not all idle
  TCP connections. Use the edge/server firewall for total connection controls.
- Rate limits are per client IP per gateway process, fixed windows, with a bounded
  10,000-entry table. New client keys fail closed when the table is full. Restart
  clears counters; replicas do not share quotas.
- Caddy overwrites `X-Real-IP`. Pingora trusts it only from `TRUSTED_PROXY_CIDRS`,
  populated from the dedicated edge subnet. Do not join untrusted services to it.
  A CDN/load balancer in front of Caddy needs a separately reviewed trusted-proxy
  configuration; the shipped config assumes Caddy is the public first hop.
- Body limits count streamed bytes. An upstream may receive a partial upload
  before a chunked request exceeds the limit; applications must not commit partial
  bodies. The limit is not a transaction boundary.
- Timeouts cover DNS and upstream connect/read/write operations; read/write limits
  are inactivity limits, not a total request-duration deadline.
- Path percent encoding, backslashes, repeated slashes and dot segments are
  rejected conservatively. Check compatibility with your API before deployment.
- Least-connections uses active upstream requests (including response streaming),
  released on completion/error/cancellation. Selection under concurrency is
  approximate. IP hash is not consistent hashing across pool membership changes.

## Verification commands

```sh
python -m pytest -q
python scripts/verify_runtime.py
bash scripts/verify_compose.sh
```

The Compose script creates disposable resources and tests a demo backend only in
its isolated CI stack. It does not deploy to your server or prove real-backend
compatibility. Run dependency/image vulnerability scans as a release gate.

Sources: [Pingora releases](https://github.com/cloudflare/pingora/releases),
[Caddy reverse proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy).
