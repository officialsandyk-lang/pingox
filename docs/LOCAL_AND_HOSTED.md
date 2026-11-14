# Local gateway and optional hosted management

## Recommended product

Keep the existing Apache-2.0 license. Offer the gateway and management server as
open-source software; sell the convenience of hosted management, backups,
monitoring retention, upgrades and support. Publishing the current private
repository is a separate release decision after a history/secret review.

The installed gateway is a background server like NGINX, not a desktop app.
Rust/Pingora handles requests locally. Python and optional AI workers are build
and management tools, not required by the compiled gateway. Start with Linux
x86_64 and ARM64 packages and OCI images; advertise each architecture only after
its build and integration checks pass. Do not require Kubernetes for a small install.

A hosted management outage must not interrupt local request handling. The planned
agent initiates outbound HTTPS connections; never expose a remote shell or public
administration port. The gateway keeps a validated last-known-good configuration.
The management service stores versioned configuration, rollout state and audit
records. Provider credentials stay in the customer's secret store. Request bodies
and authorization headers are not telemetry. AI proposes changes for review; it
is never called on the traffic path.

For an initial hosted offering, use a containerized management API and UI with
PostgreSQL on a dedicated Linux host, independent of the gateway hosts. Add
per-organization permissions, enrollment with short-lived tokens, per-device
credentials, revocation, authenticated configuration updates, monotonic revisions,
rollback and backup/restore verification before onboarding external customers.
A small standalone Go agent would be appropriate when enrollment and remote
updates are implemented; don't add another always-running process just to start
this release's gateway. Keep the management API portable across cloud hosts.

## Available native commands

From the repository's Python environment, with Rust and its native build
prerequisites installed (see PORTABILITY.md):

```sh
python gateway.py init --directory my-gateway --target systemd
# Edit my-gateway/gateway.json to use your actual backend host and port.
python gateway.py validate --config my-gateway/gateway.json
python gateway.py build --config my-gateway/gateway.json --output releases/v1
python gateway.py run --release releases/v1
```

`build` generates a fresh native release and compiles locked dependencies.
It never installs or starts a system service. Builds need dependency access unless
Cargo's cache is already populated. `run` needs no AI account or hosted service;
it verifies binary/config checksums, then replaces itself with the gateway, so
signals and exit codes are delivered directly. Checksums catch accidental edits,
not malicious replacement by someone able to rewrite the release manifest.
Run only artifacts you trust. The binary is OS/architecture specific.

The default listener is `127.0.0.1`. `--bind 0.0.0.0` explicitly exposes the HTTP
listener; use a TLS reverse proxy or private network as described in PRODUCTION.md.
For background operation, install the generated systemd unit using PORTABILITY.md.
The copied binary can run directly without Python; the unit fixes the loopback
binding. It does not run the Python manifest check. Systemd manages startup,
restart, logs and shutdown. No existing Docker workloads are modified.

## Remaining product work

The native service now supports live configuration, verified TLS/mTLS, scoped API
keys, signed bundles and reversible installation. See ENTERPRISE_DEPLOYMENT.md for
exact commands and tested boundaries. The hosted dashboard and enrollment agent
remain separate, unimplemented products. OS package repositories and automatic
fleet-wide software upgrades are not supplied.

AI worker provider support means configuration-generation integrations. It does
not yet mean that the proxy translates or normalizes every AI provider's inference
API. See PORTABILITY.md for supported protocols and gateway runtime limitations.
