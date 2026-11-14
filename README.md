# Pingox

[![Tests](https://github.com/officialsandyk-lang/pingox/actions/workflows/tests.yml/badge.svg)](https://github.com/officialsandyk-lang/pingox/actions/workflows/tests.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

**An open-source load balancer and API gateway built on [Cloudflare Pingora](https://github.com/cloudflare/pingora).**

Run Pingox on your infrastructure. Its Rust data plane handles traffic without
Python, an AI provider or a hosted account. Optional AI workers help propose
configuration; reviewed JSON works on its own.

Pingox 0.2 is a pre-1.0, self-hosted HTTP gateway release. APIs and configuration
may evolve. Passing tests establish the behavior described below, not an uptime
SLA, throughput guarantee or independent security certification.

## Features

- Round robin, weighted round robin, least active requests, IP hash and random balancing.
- Passive backend failure isolation, cooldown recovery and fail-closed unavailable pools.
- Live, revision-checked configuration with in-flight request snapshots and rollback.
- Scoped API keys, route/method restrictions and per-process quotas.
- Verified HTTPS and optional mutual TLS to backends.
- Request/body limits, timeouts, graceful shutdown and trusted-proxy handling.
- Internal readiness, health and Prometheus counters.
- Docker Compose, OCI container, Kubernetes and Linux systemd deployment generation.
- Signed native bundle tooling with verified installation and previous-release selection.

## Quick start

Use Python 3.12 or newer. Native builds also need Rust 1.95 and native build tools;
see the [deployment guide](docs/ENTERPRISE_DEPLOYMENT.md). Linux x86-64 and ARM64
have integration coverage. Windows services and serverless edge runtimes are not
supported targets.

```sh
git clone https://github.com/officialsandyk-lang/pingox.git
cd pingox
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

python pingox.py init --directory my-gateway --target systemd
# Edit my-gateway/gateway.json with your real backend addresses.
python pingox.py validate --config my-gateway/gateway.json
python pingox.py build --config my-gateway/gateway.json --output releases/v1
python pingox.py apply --config my-gateway/gateway.json --state-dir my-gateway/runtime --expect-revision 0
python pingox.py run --release releases/v1 --state-dir my-gateway/runtime
```

The native launcher binds to `127.0.0.1` by default. Use a reviewed HTTPS ingress
before exposing traffic. In another terminal, activate the same Python environment
and inspect the running configuration with `python pingox.py status --port 8080`.

For Docker Compose:

```sh
python pingox.py generate --config my-gateway/gateway.json --target compose --output releases/compose-v1
```

Follow the [Compose runbook](docs/PRODUCTION.md) to configure the domain, backend
network, approved images and TLS. Generation does not provision or deploy anything.
Use backend DNS/IP addresses reachable from the chosen deployment target.

## Documentation

| Guide | Contents |
|---|---|
| [Deployment and release lifecycle](docs/ENTERPRISE_DEPLOYMENT.md) | Live configuration, TLS/mTLS, scoped keys, signing, installation and rollback |
| [Gateway operations](docs/PINGOX_OPERATIONS.md) | Balancing, failure isolation, health and monitoring |
| [Portable deployment and providers](docs/PORTABILITY.md) | Deployment targets and optional AI provider protocols |
| [Docker Compose runbook](docs/PRODUCTION.md) | TLS ingress and Linux container operations |
| [AI workers](docs/WORKERS.md) | Bounded model calls, Codex and credential handling |
| [Hosted management direction](docs/LOCAL_AND_HOSTED.md) | Proposed hosted product; not an implemented service |
| [Security](SECURITY.md) | Trust boundaries, advisory checks and private reporting |
| [Changelog](CHANGELOG.md) | Release changes |

## Supported boundaries

The public release provides source code and build/deployment tooling. It does not
include a hosted dashboard, published container registry or prebuilt native binaries.
The signed-native workflow requires a maintainer-controlled signing key; a checksum
alone is not a publisher signature.

Live updates are local to each process; Kubernetes ConfigMap propagation is eventual.
Quotas are per process, not distributed. Listener port changes and binary upgrades
require a managed restart. A single-instance restart may interrupt traffic.
Keep all `/_gateway` endpoints private at ingress. Startup requires valid on-disk
configuration; an in-memory last-known-good snapshot is not a persistent backup.

JWT/OIDC validation, active health probes, distributed quotas, WebSockets and native
IPv6 upstream address literals are outside this release. Encoded URL paths are
rejected; query strings are forwarded. API-key admission does not replace backend
business authorization. Supported AI-worker protocols do not imply universal
translation between inference APIs.

The previous `gateway.py` entry point and existing generated service/resource names
remain compatible. Legacy `main.py`, `update.py` and self-healing workflows are
experimental development tools, not production supervisors. Older tags and
`legacy-*` branches retain historical development work.

## Verification and contributions

```sh
python -m pytest -q
python scripts/verify_runtime.py
python scripts/verify_enterprise.py
```

CI covers Linux x86-64/ARM64, native runtime behavior, Compose, Kubernetes, systemd
and Python/Rust advisory checks. The concurrency check is functional testing, not
a capacity benchmark. Validate real workloads, secrets, TLS, backups and rollback
before customer cutover. See [CONTRIBUTING.md](CONTRIBUTING.md) for test isolation
and [SECURITY.md](SECURITY.md) for remaining upstream maintenance warnings.

## License and attribution

[Apache License 2.0](LICENSE). Pingox is an independent project built on Pingora;
it is not an official Cloudflare product. Native bundles and container images
include collected dependency notices. Review the applicable licenses for your
extensions and redistribution model.
